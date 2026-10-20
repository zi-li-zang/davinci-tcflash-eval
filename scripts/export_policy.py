"""SDK image-family alignment; codec semantics remain explicit when ambiguous."""
from pathlib import Path

ALIASES = {'jpeg': 'jpg', 'tiff': 'tif'}


def family(value):
    value = str(value).lower().lstrip('.')
    return ALIASES.get(value, value)


def sdk_export_rules(rules, sdk_dir):
    rules = dict(rules)
    if rules.get('settings_source') != 'sdk_aligned':
        return rules
    from compare_outputs import image_inventory
    if not sdk_dir or not Path(sdk_dir).is_dir():
        raise ValueError('SDK_EXPORT_SOURCE_MISSING: 需提供 SDK 目录或显式导出格式/编码')
    families = {family(Path(p).suffix) for p in image_inventory(sdk_dir)}
    if len(families) != 1:
        raise ValueError('SDK_EXPORT_FORMAT_AMBIGUOUS: SDK 图像格式为空或混合；请指定时间线基准子目录或显式导出方案')
    expected = families.pop()
    if rules.get('format') and family(rules['format']) != expected:
        raise ValueError('SDK_EXPORT_FORMAT_CONFLICT: 声明导出格式与 SDK 图像格式不一致')
    rules['sdk_image_family'] = expected
    return rules


def resolve_export_rules(project, rules):
    rules = dict(rules)
    if rules.get('settings_source') != 'sdk_aligned':
        return rules
    expected = rules.get('sdk_image_family')
    if not expected:
        raise ValueError('SDK_EXPORT_SOURCE_MISSING: 未盘点 SDK 图像格式')
    formats = project.GetRenderFormats() or {}
    candidates = sorted({ext for ext in formats.values() if family(ext) == expected})
    fmt = rules.get('format')
    if fmt and fmt not in candidates:
        raise ValueError(f'SDK_EXPORT_FORMAT_UNAVAILABLE: {fmt}; 可用匹配格式={candidates}')
    if not fmt:
        if len(candidates) != 1:
            raise ValueError(f'SDK_EXPORT_FORMAT_UNAVAILABLE: SDK={expected}; 可用匹配格式={candidates}')
        fmt = candidates[0]
    codecs = project.GetRenderCodecs(fmt) or {}
    codec = rules.get('codec')
    if not codec:
        values = set(codecs.values())
        if len(values) != 1:
            raise ValueError(f'SDK_EXPORT_CODEC_REQUIRED: {fmt}; Agent 需核对 SDK 位深/通道并选择 codec，候选={codecs}')
        codec = values.pop()
    if codec not in codecs.values():
        raise ValueError(f'SDK_EXPORT_CODEC_UNAVAILABLE: {codec}; 可用={codecs}')
    rules.update(format=fmt, codec=codec, preserve_current_settings=False)
    return rules
