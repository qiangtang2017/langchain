"""Base implementation for tools or skills."""
from __future__ import annotations

import inspect
import warnings
from abc import ABC, abstractmethod
from inspect import signature
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Optional,
    Tuple,
    Type,
    Union,
)

from pydantic import (
    BaseModel,
    Extra,
    Field,
    create_model,
    root_validator,
    validate_arguments,
)
from pydantic.main import ModelMetaclass

from langchain.callbacks.base import BaseCallbackManager
from langchain.callbacks.manager import (
    AsyncCallbackManager,
    AsyncCallbackManagerForToolRun,
    CallbackManager,
    Callbacks,
)


class SchemaAnnotationError(TypeError):
    """Raised when 'args_schema' is missing or has an incorrect type annotation."""


class ToolMetaclass(ModelMetaclass):
    """Metaclass for BaseTool to ensure the provided args_schema

    doesn't silently ignored."""

    def __new__(
        cls: Type[ToolMetaclass], name: str, bases: Tuple[Type, ...], dct: dict
    ) -> ToolMetaclass:
        """Create the definition of the new tool class."""
        schema_type: Optional[Type[BaseModel]] = dct.get("args_schema")
        if schema_type is not None:
            schema_annotations = dct.get("__annotations__", {})
            args_schema_type = schema_annotations.get("args_schema", None)
            if args_schema_type is None or args_schema_type == BaseModel:
                # Throw errors for common mis-annotations.
                # TODO: Use get_args / get_origin and fully
                # specify valid annotations.
                typehint_mandate = """
class ChildTool(BaseTool):
    ...
    args_schema: Type[BaseModel] = SchemaClass
    ..."""
                raise SchemaAnnotationError(
                    f"Tool definition for {name} must include valid type annotations"
                    f" for argument 'args_schema' to behave as expected.\n"
                    f"Expected annotation of 'Type[BaseModel]'"
                    f" but got '{args_schema_type}'.\n"
                    f"Expected class looks like:\n"
                    f"{typehint_mandate}"
                )
        # Pass through to Pydantic's metaclass
        return super().__new__(cls, name, bases, dct)


def _create_subset_model(
    name: str, model: BaseModel, field_names: list
) -> Type[BaseModel]:
    """Create a pydantic model with only a subset of model's fields."""
    fields = {
        field_name: (
            model.__fields__[field_name].type_,
            model.__fields__[field_name].default,
        )
        for field_name in field_names
        if field_name in model.__fields__
    }
    return create_model(name, **fields)  # type: ignore


def get_filtered_args(
    inferred_model: Type[BaseModel],
    func: Callable,
) -> dict:
    """Get the arguments from a function's signature."""
    schema = inferred_model.schema()["properties"]
    valid_keys = signature(func).parameters
    return {k: schema[k] for k in valid_keys}


def create_schema_from_function(
    model_name: str,
    func: Callable,
) -> Type[BaseModel]:
    """Create a pydantic schema from a function's signature."""
    inferred_model = validate_arguments(func).model  # type: ignore
    # Pydantic adds placeholder virtual fields we need to strip
    filtered_args = get_filtered_args(inferred_model, func)
    return _create_subset_model(
        f"{model_name}Schema", inferred_model, list(filtered_args)
    )


class BaseTool(ABC, BaseModel, metaclass=ToolMetaclass):
    """Interface LangChain tools must implement."""

    name: str
    """The unique name of the tool that clearly communicates its purpose."""
    description: str
    """Used to tell the model how/when/why to use the tool.
    
    You can provide few-shot examples as a part of the description.
    """
    args_schema: Optional[Type[BaseModel]] = None
    """Pydantic model class to validate and parse the tool's input arguments."""
    return_direct: bool = False
    """Whether to return the tool's output directly. Setting this to True means
    
    that after the tool is called, the AgentExecutor will stop looping.
    """
    verbose: bool = False
    """Whether to log the tool's progress."""

    callbacks: Callbacks = None
    callback_manager: Optional[BaseCallbackManager] = None

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid
        arbitrary_types_allowed = True

    @property
    def is_single_input(self) -> bool:
        """Whether the tool only accepts a single input."""
        return len(self.args) == 1

    @property
    def args(self) -> dict:
        if self.args_schema is not None:
            return self.args_schema.schema()["properties"]
        else:
            inferred_model = validate_arguments(self._run).model  # type: ignore
            return get_filtered_args(inferred_model, self._run)

    def _parse_input(
        self,
        tool_input: Union[str, Dict],
    ) -> None:
        """Convert tool input to pydantic model."""
        input_args = self.args_schema
        if isinstance(tool_input, str):
            if input_args is not None:
                key_ = next(iter(input_args.__fields__.keys()))
                input_args.validate({key_: tool_input})
        else:
            if input_args is not None:
                input_args.validate(tool_input)

    @root_validator()
    def raise_deprecation(cls, values: Dict) -> Dict:
        """Raise deprecation warning if callback_manager is used."""
        if values.get("callback_manager") is not None:
            warnings.warn(
                "callback_manager is deprecated. Please use callbacks instead.",
                DeprecationWarning,
            )
            values["callbacks"] = values.pop("callback_manager", None)
        return values

    @abstractmethod
    def _run(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Use the tool.

        To enable tracing, add run_manager: Optional[CallbackManagerForToolRun] = None to child implementations.
        """

    @abstractmethod
    async def _arun(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Use the tool asynchronously.

        To enable tracing, add run_manager: Optional[AsyncCallbackManagerForToolRun] = None to child implementations.
        """

    def _to_args_and_kwargs(self, tool_input: Union[str, Dict]) -> Tuple[Tuple, Dict]:
        # For backwards compatibility, if run_input is a string,
        # pass as a positional argument.
        if isinstance(tool_input, str):
            return (tool_input,), {}
        else:
            return (), tool_input

    def run(
        self,
        tool_input: Union[str, Dict],
        verbose: Optional[bool] = None,
        start_color: Optional[str] = "green",
        color: Optional[str] = "green",
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> str:
        """Run the tool."""
        self._parse_input(tool_input)
        if not self.verbose and verbose is not None:
            verbose_ = verbose
        else:
            verbose_ = self.verbose
        callback_manager = CallbackManager.configure(
            callbacks, self.callbacks, verbose=verbose_
        )
        # TODO: maybe also pass through run_manager is _run supports kwargs
        new_arg_supported = inspect.signature(self._run).parameters.get("run_manager")
        run_manager = callback_manager.on_tool_start(
            {"name": self.name, "description": self.description},
            tool_input if isinstance(tool_input, str) else str(tool_input),
            color=start_color,
            **kwargs,
        )
        try:
            tool_args, tool_kwargs = self._to_args_and_kwargs(tool_input)
            observation = (
                self._run(*tool_args, run_manager=run_manager, **tool_kwargs)
                if new_arg_supported
                else self._run(*tool_args, **tool_kwargs)
            )
        except (Exception, KeyboardInterrupt) as e:
            run_manager.on_tool_error(e)
            raise e
        run_manager.on_tool_end(str(observation), color=color, name=self.name, **kwargs)
        return observation

    async def arun(
        self,
        tool_input: Union[str, Dict],
        verbose: Optional[bool] = None,
        start_color: Optional[str] = "green",
        color: Optional[str] = "green",
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> Any:
        """Run the tool asynchronously."""
        self._parse_input(tool_input)
        if not self.verbose and verbose is not None:
            verbose_ = verbose
        else:
            verbose_ = self.verbose
        callback_manager = AsyncCallbackManager.configure(
            callbacks, self.callbacks, verbose=verbose_
        )
        new_arg_supported = inspect.signature(self._arun).parameters.get("run_manager")
        run_manager = await callback_manager.on_tool_start(
            {"name": self.name, "description": self.description},
            tool_input if isinstance(tool_input, str) else str(tool_input),
            color=start_color,
            **kwargs,
        )
        try:
            # We then call the tool on the tool input to get an observation
            tool_args, tool_kwargs = self._to_args_and_kwargs(tool_input)
            observation = (
                await self._arun(*tool_args, run_manager=run_manager, **tool_kwargs)
                if new_arg_supported
                else await self._arun(*tool_args, **kwargs)
            )
        except (Exception, KeyboardInterrupt) as e:
            await run_manager.on_tool_error(e)
            raise e
        await run_manager.on_tool_end(
            str(observation), color=color, name=self.name, **kwargs
        )
        return observation

    def __call__(self, tool_input: str, callbacks: Callbacks = None) -> str:
        """Make tool callable."""
        return self.run(tool_input, callbacks=callbacks)


class StructuredTool(BaseTool):
    """Tool that can operate on any number of inputs."""

    description: str = ""
    args_schema: Type[BaseModel] = Field(..., description="The tool schema.")
    """The input arguments' schema."""
    func: Callable[..., Any]
    """The function to run when the tool is called."""
    coroutine: Optional[Callable[..., Awaitable[Any]]] = None
    """The asynchronous version of the function."""

    @property
    def args(self) -> dict:
        """The tool's input arguments."""
        return self.args_schema.schema()["properties"]

    def _run(
        self,
        *args: Any,
        run_manager: Optional[AsyncCallbackManagerForToolRun] = None,
        **kwargs: Any,
    ) -> Any:
        """Use the tool."""
        new_argument_supported = signature(self.func).parameters.get("callbacks")
        return (
            self.func(
                *args,
                callbacks=run_manager.get_child() if run_manager else None,
                **kwargs,
            )
            if new_argument_supported
            else self.func(*args, **kwargs)
        )

    async def _arun(
        self,
        *args: Any,
        run_manager: Optional[AsyncCallbackManagerForToolRun] = None,
        **kwargs: Any,
    ) -> str:
        """Use the tool asynchronously."""
        if self.coroutine:
            new_argument_supported = signature(self.coroutine).parameters.get(
                "callbacks"
            )
            return (
                await self.coroutine(
                    *args,
                    callbacks=run_manager.get_child() if run_manager else None,
                    **kwargs,
                )
                if new_argument_supported
                else await self.coroutine(*args, **kwargs)
            )
        raise NotImplementedError("Tool does not support async")

    @classmethod
    def from_function(
        cls,
        func: Callable,
        name: Optional[str] = None,
        description: Optional[str] = None,
        return_direct: bool = False,
        args_schema: Optional[Type[BaseModel]] = None,
        infer_schema: bool = True,
        **kwargs: Any,
    ) -> StructuredTool:
        name = name or func.__name__
        description = description or func.__doc__
        assert (
            description is not None
        ), "Function must have a docstring if description not provided."

        # Description example:
        # search_api(query: str) - Searches the API for the query.
        description = f"{name}{signature(func)} - {description.strip()}"
        _args_schema = args_schema
        if _args_schema is None and infer_schema:
            _args_schema = create_schema_from_function(f"{name}Schema", func)
        return cls(
            name=name,
            func=func,
            args_schema=_args_schema,
            description=description,
            return_direct=return_direct,
            **kwargs,
        )
