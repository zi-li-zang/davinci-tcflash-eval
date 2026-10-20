"""Read Color graph tool identities, never infer OFX presence from node labels."""
IDENTITIES = {'truecut flash', 'com.pixelworks.truecutflash.ofx'}


def inspect_timeline(project, timeline):
    evidence, errors, matches = [], [], []

    def scan(get_graph, scope):
        try:
            graph = get_graph()
            if graph is None:
                raise ValueError('node graph unavailable')
            count = graph.GetNumNodes()
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError('node count unavailable')
            for index in range(1, count + 1):
                tools = graph.GetToolsInNode(index)
                if not isinstance(tools, list) or not all(isinstance(x, str) for x in tools):
                    errors.append({'scope': scope, 'node_index': index, 'reason': 'node tools unavailable'})
                    continue
                record = {'scope': scope, 'node_index': index, 'tools': tools}
                evidence.append(record)
                for tool in tools:
                    name = tool.strip().casefold()
                    if name.startswith('ofx:'):
                        name = name[4:].strip()
                    if name in IDENTITIES:
                        matches.append(record)
        except Exception as exc:
            errors.append({'scope': scope, 'reason': str(exc)})

    scan(timeline.GetNodeGraph, 'timeline')
    try:
        layers = int(project.GetSetting('nodeStackLayers'))
        if layers < 1:
            raise ValueError('invalid nodeStackLayers')
    except Exception as exc:
        layers = 1
        errors.append({'scope': 'layers', 'reason': str(exc)})
    try:
        count = timeline.GetTrackCount('video')
        if not isinstance(count, int) or count < 0:
            raise ValueError('track count unavailable')
        for track in range(1, count + 1):
            clips = timeline.GetItemListInTrack('video', track)
            if not isinstance(clips, list):
                raise ValueError('clip list unavailable')
            for n, clip in enumerate(clips, 1):
                scope = f'track:{track}/clip:{n}'
                for layer in range(1, layers + 1):
                    scan(lambda c=clip, l=layer: c.GetNodeGraph(l), f'{scope}/layer:{layer}')
                try:
                    group = clip.GetColorGroup()
                    if group is not None:
                        scan(group.GetPreClipNodeGraph, scope + '/group-pre')
                        scan(group.GetPostClipNodeGraph, scope + '/group-post')
                except Exception as exc:
                    errors.append({'scope': scope + '/group', 'reason': str(exc)})
    except Exception as exc:
        errors.append({'scope': 'clips', 'reason': str(exc)})
    return {'status': 'PRESENT' if matches else ('UNKNOWN' if errors else 'ABSENT'),
            'matches': matches, 'nodes': evidence, 'errors': errors,
            'scope': 'Color timeline, clip stack layers and assigned pre/post groups; existence only, not enabled state or inference proof'}
