import json

from openai import BadRequestError


def create_json_completion(client, *, messages, response_format, **kwargs):
    """Use strict output when available, with JSON mode for compatible services."""
    if getattr(client, "_alexandria_json_object", False) is not True:
        try:
            return client.chat.completions.create(messages=messages, response_format=response_format, **kwargs)
        except BadRequestError as exc:
            message = str(exc).lower()
            if not (any(name in message for name in ("response_format", "json_schema"))
                    and any(reason in message for reason in ("unavailable", "unsupported", "not supported", "not support", "not available", "not allowed"))):
                raise
            client._alexandria_json_object = True
            print("LLM 服务不支持 JSON Schema，已切换为兼容 JSON 模式。")

    schema = response_format["json_schema"]["schema"]
    array_output = schema["type"] == "array"
    if array_output:
        schema = {
            "type": "object", "properties": {"items": schema},
            "required": ["items"], "additionalProperties": False,
        }
    instruction = (
        "\n\nOUTPUT FORMAT OVERRIDE: Return only a JSON object matching the following JSON Schema. "
        "This overrides earlier output-format instructions. "
        + ('Wrap the requested array in the "items" property; include every required entry. ' if array_output else '')
        + json.dumps(schema, ensure_ascii=False)
    )
    request_messages = [dict(message) for message in messages]
    if request_messages and request_messages[0]["role"] == "system":
        request_messages[0]["content"] += instruction
    else:
        request_messages.insert(0, {"role": "system", "content": instruction})
    response = client.chat.completions.create(
        messages=request_messages, response_format={"type": "json_object"}, **kwargs,
    )
    if array_output:
        content = json.loads(response.choices[0].message.content or "")
        if not isinstance(content, dict) or set(content) != {"items"} or not isinstance(content["items"], list):
            raise ValueError("LLM JSON response must contain an items array")
        response.choices[0].message.content = json.dumps(content["items"], ensure_ascii=False)
    return response
