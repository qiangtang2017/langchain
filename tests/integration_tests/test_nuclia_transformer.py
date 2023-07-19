import asyncio
import json
import os
import pytest
from typing import Any
from unittest import mock

from langchain.document_loaders.nuclia import NucliaLoader
from langchain.document_transformers.nuclia_text_transform import NucliaTextTransformer
from langchain.schema.document import Document
from langchain.tools.nuclia.tool import NucliaUnderstandingAPI


def fakerun(**args: Any) -> Any:
    async def run(self: Any, **args: Any) -> str:
        asyncio.sleep(0.1)
        data = {
            "extracted_text": [{"body": {"text": "Hello World"}}],
            "file_extracted_data": [{"language": "en"}],
            "field_metadata": [
                {
                    "metadata": {
                        "metadata": {
                            "paragraphs": [
                                {"end": 66, "sentences": [{"start": 1, "end": 67}]}
                            ]
                        }
                    }
                }
            ],
        }
        return json.dumps(data)

    return run


@pytest.mark.asyncio
@mock.patch.dict(os.environ, {"NUCLIA_NUA_KEY": "_a_key_"})
async def test_nuclia_loader() -> None:
    with mock.patch(
        "langchain.tools.nuclia.tool.NucliaUnderstandingAPI._arun", new_callable=fakerun
    ):
        nua = NucliaUnderstandingAPI(enable_ml=False)
        documents = [
            Document(page_content="Hello, my name is Alice", metadata={}),
            Document(page_content="Hello, my name is Bob", metadata={}),
        ]
        nuclia_transformer = NucliaTextTransformer(nua)
        transformed_documents = await nuclia_transformer.atransform_documents(documents)
        assert len(transformed_documents) == 2
        assert transformed_documents[0].metadata["nuclia"]["file"]["language"] == "en"
        assert (
            len(transformed_documents[1].metadata["nuclia"]["metadata"]["metadata"]["metadata"]["paragraphs"]) == 1
        )
