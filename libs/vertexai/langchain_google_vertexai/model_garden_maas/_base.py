import copy
from enum import Enum, auto
from typing import (
    Any,
    AsyncContextManager,
    AsyncIterator,
    Callable,
    Dict,
    Optional,
    Union,
)

import httpx  # type: ignore[import-not-found]
from google import auth
from google.auth.credentials import Credentials
from google.auth.transport import requests as auth_requests
from httpx_sse import (  # type: ignore[import-not-found]
    EventSource,
    aconnect_sse,
    connect_sse,
)
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.llms import create_base_retry_decorator
from langchain_core.pydantic_v1 import root_validator

from langchain_google_vertexai._base import _VertexAIBase


def _get_token(credentials: Optional[Credentials] = None) -> str:
    """Returns a valid token for GCP auth."""
    credentials = auth.default()[0] if not credentials else credentials
    request = auth_requests.Request()
    credentials.refresh(request)
    if not credentials.token:
        raise ValueError("Couldn't retrieve a token!")
    return credentials.token


def _raise_on_error(response: httpx.Response) -> None:
    """Raise an error if the response is an error."""
    if httpx.codes.is_error(response.status_code):
        error_message = response.read().decode("utf-8")
        raise httpx.HTTPStatusError(
            f"Error response {response.status_code} "
            f"while fetching {response.url}: {error_message}",
            request=response.request,
            response=response,
        )


async def _araise_on_error(response: httpx.Response) -> None:
    """Raise an error if the response is an error."""
    if httpx.codes.is_error(response.status_code):
        error_message = (await response.aread()).decode("utf-8")
        raise httpx.HTTPStatusError(
            f"Error response {response.status_code} "
            f"while fetching {response.url}: {error_message}",
            request=response.request,
            response=response,
        )


async def _aiter_sse(
    event_source_mgr: AsyncContextManager[EventSource],
) -> AsyncIterator[Dict]:
    """Iterate over the server-sent events."""
    async with event_source_mgr as event_source:
        await _araise_on_error(event_source.response)
        async for event in event_source.aiter_sse():
            if event.data == "[DONE]":
                return
            yield event.json()


class VertexMaaSModelFamily(str, Enum):
    LLAMA = auto()
    # https://cloud.google.com/blog/products/ai-machine-learning/llama-3-1-on-vertex-ai
    MISTRAL = auto()
    # https://cloud.google.com/blog/products/ai-machine-learning/codestral-and-mistral-large-v2-on-vertex-ai

    @classmethod
    def _missing_(cls, value: Any) -> "VertexMaaSModelFamily":
        model_name = value.lower()
        llama_models = {
            "meta/llama3-405b-instruct-maas",
        }
        mistral_models = {"mistral-nemo@2407", "mistral-large@2407"}
        if model_name in llama_models:
            return VertexMaaSModelFamily.LLAMA
        if model_name in mistral_models:
            return VertexMaaSModelFamily.MISTRAL
        raise ValueError(f"Model {model_name} is not supported yet!")


class _BaseVertexMaasModelGarden(_VertexAIBase):
    append_tools_to_system_message: bool = False
    "Whether to append tools to the system message or not."
    model_family: Optional[VertexMaaSModelFamily] = None

    class Config:
        """Configuration for this pydantic object."""

        allow_population_by_field_name = True
        arbitrary_types_allowed = True

    @root_validator(pre=True)
    def validate_environment_model_garden(cls, values: Dict) -> Dict:
        """Validate that the python package exists in environment."""
        family = VertexMaaSModelFamily(values["model_name"])
        values["model_family"] = family
        if family == VertexMaaSModelFamily.MISTRAL:
            model = values["model_name"].split("@")[0]
            values["full_model_name"] = values["model_name"]
            values["model_name"] = model
        return values

    def _enrich_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fix params to be compliant with Vertex AI."""
        copy_params = copy.deepcopy(params)
        _ = copy_params.pop("safe_prompt", None)
        copy_params["model"] = self.model_name
        return copy_params

    def _get_url_part(self, stream: bool = False) -> str:
        if self.model_family == VertexMaaSModelFamily.MISTRAL:
            if stream:
                return (
                    f"publishers/mistralai/models/{self.full_model_name}"
                    ":streamRawPredict"
                )
            return f"publishers/mistralai/models/{self.full_model_name}:rawPredict"
        return "openapi/chat/completions"

    def get_url(self) -> str:
        if self.model_family == VertexMaaSModelFamily.LLAMA:
            version = "v1beta1"
        else:
            version = "v1"
        return (
            f"https://{self.location}-aiplatform.googleapis.com/{version}/projects/"
            f"{self.project}/locations/{self.location}"
        )


def _create_retry_decorator(
    llm: _BaseVertexMaasModelGarden,
    run_manager: Optional[
        Union[AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun]
    ] = None,
) -> Callable[[Any], Any]:
    """Returns a tenacity retry decorator, preconfigured to handle exceptions"""

    errors = [httpx.RequestError, httpx.StreamError]
    return create_base_retry_decorator(
        error_types=errors, max_retries=llm.max_retries, run_manager=run_manager
    )


async def acompletion_with_retry(
    llm: _BaseVertexMaasModelGarden,
    run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
    **kwargs: Any,
) -> Any:
    """Use tenacity to retry the async completion call."""
    retry_decorator = _create_retry_decorator(llm, run_manager=run_manager)

    @retry_decorator
    async def _completion_with_retry(**kwargs: Any) -> Any:
        if "stream" not in kwargs:
            kwargs["stream"] = False
        stream = kwargs["stream"]
        if stream:
            event_source = aconnect_sse(
                llm.async_client,
                "POST",
                llm._get_url_part(stream=True),
                json=kwargs,
                headers={"Accept": "text/event-stream"},
            )
            return _aiter_sse(event_source)
        else:
            response = await llm.async_client.post(url=llm._get_url_part(), json=kwargs)
            await _araise_on_error(response)
            return response.json()

    kwargs = llm._enrich_params(kwargs)
    return await _completion_with_retry(**kwargs)


def completion_with_retry(llm: _BaseVertexMaasModelGarden, **kwargs):
    if "stream" not in kwargs:
        kwargs["stream"] = False
    stream = kwargs["stream"]
    kwargs = llm._enrich_params(kwargs)

    if stream:

        def iter_sse():
            with connect_sse(
                llm.client,
                "POST",
                llm._get_url_part(stream=True),
                json=kwargs,
                headers={"Accept": "text/event-stream"},
            ) as event_source:
                _raise_on_error(event_source.response)
                for event in event_source.iter_sse():
                    if event.data == "[DONE]":
                        return
                    yield event.json()

        return iter_sse()
    response = llm.client.post(url=llm._get_url_part(), json=kwargs)
    _raise_on_error(response)
    return response.json()