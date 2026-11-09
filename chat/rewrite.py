"""Question rewriting: turn a question into a plain-English search query before
searching (Pidgin, paraphrases such as "what time do you close", follow-ups)."""
import llm


async def rewrite(model: str, question: str, previous: str | None, cfg) -> tuple[str | None, int, int]:
    """(search query or None, input tokens, output tokens). Never raises."""
    user = f"Earlier question: {previous}\nLatest question: {question}" if previous else question
    out, prompt_tokens, completion_tokens = "", 0, 0
    try:
        async for event in llm.stream_chat(
            model, [{"role": "system", "content": cfg.rewrite_prompt}, {"role": "user", "content": user}],
            temperature=0, max_tokens=60,
        ):
            if event["type"] == "content":
                out += event["text"]
            elif event["type"] == "done":
                prompt_tokens = event.get("prompt_tokens") or 0
                completion_tokens = event.get("completion_tokens") or 0
    except llm.LLMError:
        return None, 0, 0
    query = out.strip().strip('"').strip("'").splitlines()[0].strip() if out.strip() else ""
    if not query or len(query) > 300:
        return None, prompt_tokens, completion_tokens
    return query, prompt_tokens, completion_tokens
