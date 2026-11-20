"""Question rewriting: turn a question into a plain-English search query before
searching (Pidgin, paraphrases such as "what time do you close", follow-ups)."""
import llm


# The earlier answer is cut to this many characters: enough to resolve "the second one" or "yes".
MAX_ANSWER_CHARS = 800


def rewrite_input(question: str, previous: str | None = None, answer: str | None = None, page: str = "") -> str:
    """What the rewriting model is given: the page a Moodle student is viewing, the earlier
    question and the assistant's answer to it (for follow-ups like "yes" or "the second one"),
    then the latest question. Just the question when there is no such context."""
    lines = []
    if page:
        lines.append(f"Page the user is viewing: {page}")
    if previous:
        lines.append(f"Earlier question: {previous}")
    if previous and answer:
        answer = " ".join(answer.split())
        if len(answer) > MAX_ANSWER_CHARS:
            answer = answer[:MAX_ANSWER_CHARS].rsplit(" ", 1)[0] + " …"
        lines.append(f"Assistant's answer: {answer}")
    if not lines:
        return question
    return "\n".join(lines + [f"Latest question: {question}"])


async def rewrite(model: str, question: str, previous: str | None, cfg, answer: str | None = None,
                  page: str = "") -> tuple[str | None, int, int]:
    """(search query or None, input tokens, output tokens). Never raises."""
    user = rewrite_input(question, previous, answer, page)
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
