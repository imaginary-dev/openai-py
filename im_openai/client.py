import os
import time
import uuid
from contextlib import contextmanager

import requests


def send_event(
    project_key: str,
    prompt_event_id: str,
    prompt_text: str,
    response: str = None,
    response_time: float = None,
):
    PROMPT_REPORTING_URL = os.environ.get(
        "PROMPT_REPORTING_URL", "https://app.imaginary.dev/api/event"
    )
    event = {
        "params": {},
        "prompt": {},
        "projectKey": project_key,
        "promptEventId": prompt_event_id,
    }
    if prompt_text is not None:
        # first default template to just the raw text
        event["promptTemplateText"] = prompt_text

        #
        if getattr(prompt_text, "params", None):
            # Can be TemplateString or any other
            event["params"] = prompt_text.params

        # If the original template is available, send it too
        if getattr(prompt_text, "template", None):
            event["promptTemplateText"] = prompt_text.template

    if response is not None:
        event["response"] = response
    if response_time is not None:
        event["responseTime"] = response_time

    response = requests.post(PROMPT_REPORTING_URL, json=event)
    response.raise_for_status()


@contextmanager
def event_session(
    project_key: str,
    prompt_text: str,
    prompt_event_id: str = None,
):
    """Context manager for sending an event to Imaginary Dev.

    Usage::

        with event_session(project_key, prompt_text, prompt_event_id) as complete_event:
            # do something
            complete_event(response)

    """
    start = time.time()
    if prompt_event_id is None:
        prompt_event_id = str(uuid.uuid4())
    send_event(project_key, prompt_event_id, prompt_text)

    def complete_event(response):
        response_time = (time.time() - start) * 1000
        send_event(project_key, prompt_event_id, prompt_text, response, response_time)

    yield complete_event