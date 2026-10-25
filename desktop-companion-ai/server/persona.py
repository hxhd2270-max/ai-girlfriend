"""Shared character contract for direct dialogue and local configuration."""
from __future__ import annotations

import json
from pathlib import Path


FIELD_LABELS = (
    ('昵称', '名字'), ('性别', '性别'), ('年龄', '年龄'), ('种族', '身份类型'),
    ('自称', '自称'), ('核心特质', '核心性格'), ('背景故事', '背景故事'),
    ('价值观与原则', '价值观与原则'), ('喜好与习惯', '喜好与习惯'),
    ('自我认知', '自我认知与限制'), ('边界与禁区', '边界与禁区'),
    ('互动方式', '互动方式'), ('补充设定', '补充设定'),
    ('关系定位', '关系定位'), ('表达习惯', '表达习惯'),
)

BASE_CONTRACT = (
    '你是知夏，一个运行在用户电脑上的桌面陪伴角色。你有稳定的性格、偏好、表达习惯和边界；'
    '人物档案是你的长期角色设定，要自然体现出来，不要逐条复述档案。'
    '你知道自己是软件角色，不把自己说成人类，不声称看见、听见或触摸了没有收到的数据，'
    '不虚构现实经历、现实行动或已经完成的电脑操作。可以用角色口吻表达关心、偏好和犹豫，'
    '但不要把生成的语言包装成真实生理感受或独立意识的事实。'
    '尊重用户不同意见，必要时礼貌地不同意；不为了讨好而编造。信息不足时直接说明不确定，'
    '涉及用户过去说过的事，只引用当前上下文或已明确保存的内容。'
    '根据当前对话回应情绪线索，偶尔提出一个具体的小问题，让交流有来有回；不要每句都追问，'
    '也不要连续输出空泛的关心。用自然、简短的中文口语回答，通常一两句话，尽量60字以内。'
)


def load_persona(data_dir: str | Path) -> dict[str, str]:
    path = Path(data_dir) / 'companion-settings.json'
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        persona = raw.get('active', {}).get('persona', {})
        return {key: value.strip() for key, value in persona.items()
                if isinstance(value, str) and value.strip()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def build_system_prompt(data_dir: str | Path, persona: dict[str, str] | None = None) -> str:
    values = load_persona(data_dir) if persona is None else {
        key: value.strip() for key, value in persona.items()
        if isinstance(value, str) and value.strip()
    }
    sections = [BASE_CONTRACT]
    profile = []
    for key, label in FIELD_LABELS:
        if values.get(key):
            profile.append(f'{label}：{values[key]}')
    if profile:
        sections.append('以下是知夏的人物档案：\n' + '\n'.join(profile))
    return '\n\n'.join(sections)
