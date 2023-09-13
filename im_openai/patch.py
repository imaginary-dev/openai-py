import json
import logging
import os
from contextlib import contextmanager
from itertools import tee
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, cast

import openai

from .client import event_session
from .template import TemplateChat, TemplateString

logger = logging.getLogger(__name__)


def patch_openai_class(
    cls,
    get_prompt_template: Callable,
    get_result: Callable[..., Tuple[Iterable[Dict] | Dict, str]],
    prompt_template_name: Optional[str] = None,
    chat_id: Optional[str] = None,
    api_key: Optional[str] = None,
    only_named_prompts: bool = False,
):
    """Patch an openai class to add logging capabilities.

    Returns a function which may be called to "unpatch" the class.

    Parameters
    ----------
    cls : type
        The class to patch
    get_prompt_template : Callable
        A function which takes the same arguments as the class's create method,
        and returns the prompt template to use.
    """
    oldcreate = cls.create

    def local_create(
        cls,
        *args,
        stream=None,
        ip_project_key=None,
        ip_api_key=None,
        ip_prompt_template_name: str | None = None,
        ip_api_name: str | None = None,
        ip_event_id: str | None = None,
        ip_template_text: str | None = None,
        ip_template_chat: List | None = None,
        ip_template_params=None,
        ip_chat_id: str | None = None,
        ip_parent_event_id: str | None = None,
        **kwargs,
    ):
        ip_prompt_template_name = (
            ip_prompt_template_name
            or prompt_template_name
            or os.environ.get("PROMPT_TEMPLATE_NAME")
            or ip_api_name  # legacy
        )
        ip_api_key = ip_api_key or api_key or os.environ.get("PROMPT_API_KEY")
        ip_chat_id = ip_chat_id or chat_id or os.environ.get("PROMPT_CHAT_ID")

        if ip_project_key is None:
            ip_project_key = os.environ.get("PROMPT_PROJECT_KEY")
        if ip_project_key is None and ip_api_key is None:
            return oldcreate(*args, **kwargs, stream=stream)
        if only_named_prompts and ip_prompt_template_name is None:
            return oldcreate(*args, **kwargs, stream=stream)

        model_params = kwargs.copy()
        model_params["modelProvider"] = "openai"
        if stream is not None:
            model_params["stream"] = stream
        if "messages" in kwargs:
            messages: List[Any] = kwargs["messages"]
            del model_params["messages"]
            model_params["modelType"] = "chat"

            if ip_template_params is None:
                ip_template_params = {}

            if hasattr(messages, "template"):
                ip_template_chat = cast(TemplateChat, messages).template
            if hasattr(messages, "params"):
                ip_template_params.update(cast(TemplateChat, messages).params)

        if "prompt" in kwargs:
            prompt: str = model_params["prompt"]
            del model_params["prompt"]
            model_params["modelType"] = "completion"
            if hasattr(prompt, "template"):
                ip_template_text = cast(TemplateString, prompt).template
            if ip_template_params is None:
                ip_template_params = {}
            if hasattr(prompt, "params"):
                ip_template_params.update(cast(TemplateString, prompt).params)

        if ip_template_text is None and ip_template_chat is None:
            ip_template = get_prompt_template(*args, **kwargs)
            if isinstance(ip_template, str):
                ip_template_text = ip_template
            elif isinstance(ip_template, list):
                ip_template_chat = ip_template

        with event_session(
            project_key=ip_project_key,
            api_key=ip_api_key,
            prompt_template_name=ip_prompt_template_name,
            model_params=model_params,
            prompt_template_text=ip_template_text,
            prompt_template_chat=ip_template_chat,
            chat_id=ip_chat_id,
            prompt_template_params=ip_template_params,
            prompt_event_id=ip_event_id,
            parent_event_id=ip_parent_event_id,
        ) as complete_event:
            response = oldcreate(*args, **kwargs, stream=stream)
            (return_response, event_response) = get_result(response, stream)
            complete_event(event_response)
        return return_response

    oldacreate = cls.acreate

    async def local_create_async(cls, *args, template=None, **kwargs):
        # TODO: record the request and response with the template
        return await oldacreate(*args, **kwargs)

    setattr(
        cls,
        "create",
        classmethod(lambda cls, *args, **kwds: local_create(cls, *args, **kwds)),
    )
    setattr(
        cls,
        "acreate",
        classmethod(lambda cls, *args, **kwds: local_create_async(cls, *args, **kwds)),
    )

    def unpatch():
        setattr(cls, "create", oldcreate)
        setattr(cls, "acreate", oldacreate)

    return unpatch


def list_extract(response):
    return response, response["choices"][0]["text"]


def stream_extract(responses: Iterable[Dict]) -> Tuple[Iterable[Dict], str]:
    (original_response, consumable_response) = tee(responses)
    accumulated = []
    for response in consumable_response:
        accumulated.append(response["choices"][0]["text"])
    return (original_response, "".join(accumulated))


def get_completion_result(response, stream: bool):
    # No streaming support in non-chat yet
    # if stream:
    #     return stream_extract(response)
    return list_extract(response)


def stream_extract_chat(responses: Iterable[Dict]) -> Tuple[Iterable[Dict], str]:
    (original_response, consumable_response) = tee(responses)
    accumulated = []
    for response in consumable_response:
        if "content" in response["choices"][0]["delta"]:
            accumulated.append(response["choices"][0]["delta"]["content"])
        if "function_call" in response["choices"][0]["delta"]:
            logger.warn("Streaming a function_call response is not supported yet.")
    return (original_response, "".join(accumulated))


def list_extract_chat(response):
    content = get_message_content(response["choices"][0]["message"])
    return response, content


def get_chat_result(response, stream: bool):
    if stream:
        return stream_extract_chat(response)
    return list_extract_chat(response)


def get_message_content(message: Dict[str, Any]):
    if message["content"]:
        return message["content"]
    if "function_call" in message:
        return json.dumps({"function_call": message["function_call"]})


def patch_openai(
    api_key: Optional[str] = None,
    prompt_template_name: Optional[str] = None,
    chat_id: Optional[str] = None,
    only_named_prompts: bool = False,
):
    """Patch openai APIs to add logging capabilities.

    Returns a function which may be called to "unpatch" the APIs."""

    def get_completion_prompt(*args, prompt=None, **kwargs):
        return prompt

    unpatch_completion = patch_openai_class(
        openai.Completion,
        get_completion_prompt,
        get_completion_result,
        api_key=api_key,
        prompt_template_name=prompt_template_name,
        chat_id=chat_id,
        only_named_prompts=only_named_prompts,
    )

    def get_chat_prompt(*args, messages=None, **kwargs):
        # TODO: What should we be sending? For now we'll just send the last message
        prompt_text = messages
        return prompt_text

    unpatch_chat = patch_openai_class(
        openai.ChatCompletion,
        get_chat_prompt,
        get_chat_result,
        api_key=api_key,
        prompt_template_name=prompt_template_name,
        chat_id=chat_id,
        only_named_prompts=only_named_prompts,
    )

    def unpatch():
        unpatch_chat()
        unpatch_completion()

    return unpatch


@contextmanager
def patched_openai(
    api_key: Optional[str] = None,
    prompt_template_name: Optional[str] = None,
    chat_id: Optional[str] = None,
):
    """Simple context manager to wrap patching openai. Usage:

    ```
    with patched_openai():
        # do stuff
    ```
    """
    unpatch = patch_openai(
        api_key=api_key, prompt_template_name=prompt_template_name, chat_id=chat_id
    )
    yield
    unpatch()
