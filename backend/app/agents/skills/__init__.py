"""Fixed application-owned methodology resources, not business evidence or plugins."""
from hashlib import sha256
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_NAME = 'travel-planning'
_RESOURCES = {'main': ('SKILL.md', 16384)}


def read_skill_resource(name: str, resource: str = 'main') -> dict:
    if name != _NAME or resource not in _RESOURCES:
        raise ValueError('未知规划技能或资源，请使用目录中的名称和资源标识')
    relative, maximum = _RESOURCES[resource]
    try:
        root = (_ROOT / _NAME).resolve(strict=True)
        # The skill directory itself must not redirect outside the resource root.
        if not root.is_relative_to(_ROOT.resolve()):
            raise ValueError('规划技能资源不可用')
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError('规划技能资源不可用')
        raw = path.read_bytes()
        if len(raw) > maximum:
            raise ValueError('规划技能资源过大，未加载')
        content = raw.decode('utf-8')
    except (OSError, UnicodeError, RuntimeError):
        raise ValueError('规划技能资源不可用，未加载') from None
    return {'name': name, 'resource': resource, 'version': '2', 'content': content,
            'hash': sha256(raw).hexdigest()}


def skill_catalog() -> tuple[dict, ...]:
    """Read metadata from the same maintained document as the full skill."""
    content = read_skill_resource(_NAME)['content']
    lines = content.splitlines()
    if not lines or lines[0] != '---' or '---' not in lines[1:]:
        raise ValueError('规划技能元数据无效')
    metadata = {}
    for line in lines[1:lines.index('---', 1)]:
        key, separator, value = line.partition(':')
        if not separator or key not in {'name', 'description'} or key in metadata:
            raise ValueError('规划技能元数据无效')
        metadata[key] = value.strip()
    if metadata.get('name') != _NAME or not metadata.get('description'):
        raise ValueError('规划技能元数据无效')
    return ({**metadata, 'resources': list(_RESOURCES)},)


def skill_catalog_prompt() -> str:
    rows = ['# 可按需读取的规划技能']
    for item in skill_catalog():
        rows.append(f"- {item['name']}：{item['description']} 资源：{', '.join(item['resources'])}。")
    return '\n'.join(rows)
