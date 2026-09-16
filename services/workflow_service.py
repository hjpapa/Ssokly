import re


SECTION_ALIASES = {
    "schedule": ("일정 메모", "기한"),
    "checklist": ("체크리스트",),
    "message": ("전달 문구", "가정통신문 초안", "교직원 메신저용 안내문"),
    "follow_up": ("첨부파일별 후속 실행", "후속 실행"),
}


def extract_section(markdown: str, section_key: str) -> str:
    aliases = SECTION_ALIASES.get(section_key, ())
    for heading in aliases:
        match = re.search(
            rf"(?ms)^##\s+{re.escape(heading)}\s*$\n?(.*?)(?=^##\s+|\Z)",
            markdown,
        )
        if match:
            content = match.group(1).strip()
            if content:
                return f"## {heading}\n{content}"
    if section_key == "checklist":
        match = re.search(r"(?ms)^## 업무 순서\s*$\n?(.*?)(?=^##\s+|\Z)", markdown)
        if match:
            tasks = re.findall(r"(?m)^\d+\. (.+)$", match.group(1))
            if tasks:
                return "## 체크리스트\n" + "\n".join(f"- [ ] {task}" for task in tasks)
    return ""
