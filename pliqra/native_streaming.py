"""Bounded native SSE translation. A missing terminal event never becomes success."""
import codecs
import json

from .connectors import MAX_RESPONSE_BYTES, MAX_STREAM_LINE_BYTES, bounded_lines, strict_json, sse, sse_data


def count(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError('Invalid native token count')
    return value


class Upstream:
    def __init__(self, provider, request_id, model):
        self.provider, self.request_id, self.model = provider, request_id, model
        self.started = self.terminal = False
        self.blocks = {}
        self.input_tokens = self.output_tokens = None
        self.reason = None
        self.size = 0
        self.text = ''
        self.parts = set()

    def chunk(self, delta=None, reason=None, usage=None):
        result = {'id': self.request_id, 'object': 'chat.completion.chunk', 'model': self.model,
                  'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': reason}]}
        if usage is not None:
            result['usage'] = usage
        return result

    def feed(self, obj):
        self.size += len(json.dumps(obj, ensure_ascii=False).encode())
        if self.size > MAX_RESPONSE_BYTES:
            raise ValueError('Native stream exceeded its size limit')
        if not isinstance(obj, dict) or obj.get('error') or obj.get('type') in ('error', 'response.failed'):
            raise ValueError('Native provider reported an error')
        if self.terminal:
            raise ValueError('Native event followed a terminal event')
        return self.anthropic(obj) if self.provider == 'anthropic' else self.responses(obj)

    def usage(self):
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return {'prompt_tokens': self.input_tokens, 'completion_tokens': self.output_tokens,
                'total_tokens': self.input_tokens + self.output_tokens}

    def index(self, obj, field):
        index = count(obj[field])
        if index >= 256:
            raise ValueError('Too many native output blocks')
        return index

    def anthropic(self, obj):
        kind = obj['type']
        if kind == 'ping':
            return []
        if kind == 'message_start':
            if self.started:
                raise ValueError('Duplicate native message start')
            self.started = True
            usage = obj['message'].get('usage') or {}
            if 'input_tokens' in usage:
                self.input_tokens = count(usage['input_tokens'])
            if 'output_tokens' in usage:
                self.output_tokens = count(usage['output_tokens'])
            return [self.chunk({'role': 'assistant'})]
        if not self.started:
            raise ValueError('Native message has not started')
        if kind == 'content_block_start':
            index = self.index(obj, 'index')
            if index in self.blocks:
                raise ValueError('Duplicate native block')
            block = dict(obj['content_block'])
            block.update(closed=False, arguments='')
            self.blocks[index] = block
            if block['type'] == 'text':
                block['parts'] = {}
                return self.text_delta(block,index,0,block.get('text',''))
            if block['type'] == 'tool_use':
                if not isinstance(block.get('id'), str) or not isinstance(block.get('name'), str):
                    raise ValueError('Invalid native tool identity')
                if block.get('input'):
                    block['arguments'] = json.dumps(block['input'], allow_nan=False)
                return [self.chunk({'tool_calls': [{'index': index, 'id': block['id'], 'type': 'function',
                                                    'function': {'name': block['name'], 'arguments': block['arguments']}}]})]
            raise ValueError('Unsupported native streaming block')
        if kind == 'content_block_delta':
            index = self.index(obj, 'index')
            block = self.blocks[index]
            if block['closed']:
                raise ValueError('Delta followed a closed native block')
            delta = obj['delta']
            if delta['type'] == 'text_delta' and block['type'] == 'text':
                if not isinstance(delta['text'], str):
                    raise ValueError('Invalid native text delta')
                return self.text_delta(block,index,0,delta['text'])
            if delta['type'] == 'input_json_delta' and block['type'] == 'tool_use':
                value = delta['partial_json']
                if not isinstance(value, str):
                    raise ValueError('Invalid native tool delta')
                block['arguments'] += value
                return [self.chunk({'tool_calls': [{'index': index, 'function': {'arguments': value}}]})]
            raise ValueError('Native delta does not match its block')
        if kind == 'content_block_stop':
            index = self.index(obj, 'index')
            block = self.blocks[index]
            if block['closed']:
                raise ValueError('Duplicate native block stop')
            block['closed'] = True
            if block['type'] == 'tool_use':
                if not isinstance(strict_json(block['arguments'] or '{}'), dict):
                    raise ValueError('Native tool arguments must be objects')
                if not block['arguments']:
                    return [self.chunk({'tool_calls': [{'index': index, 'function': {'arguments': '{}'}}]})]
            return []
        if kind == 'message_delta':
            self.reason = obj['delta'].get('stop_reason', self.reason)
            usage = obj['usage']
            # The start event's output count is partial, never final billing evidence.
            self.output_tokens = count(usage['output_tokens'])
            if 'input_tokens' in usage:
                self.input_tokens = count(usage['input_tokens'])
            return []
        if kind == 'message_stop':
            if not self.reason or any(not b['closed'] for b in self.blocks.values()):
                raise ValueError('Native message stopped before its blocks completed')
            self.terminal = True
            reason = 'tool_calls' if self.reason == 'tool_use' else 'length' if self.reason == 'max_tokens' else 'stop'
            return [self.chunk(reason=reason, usage=self.usage())]
        return []

    def responses(self, obj):
        kind = obj['type']
        if kind in ('response.created', 'response.in_progress'):
            if self.started:
                return []
            self.started = True
            return [self.chunk({'role': 'assistant'})]
        if not self.started:
            raise ValueError('Native response has not started')
        if kind == 'response.output_item.added':
            index = self.index(obj, 'output_index')
            if index in self.blocks:
                raise ValueError('Duplicate native output item')
            block = dict(obj['item'])
            block.update(arguments='', text='', closed=False,parts={})
            if block['type'] not in ('message', 'function_call'):
                raise ValueError('Unsupported native output item')
            self.blocks[index] = block
            if block['type'] == 'function_call':
                return [self.chunk({'tool_calls': [{'index': index, 'id': block['call_id'], 'type': 'function',
                                                    'function': {'name': block['name'], 'arguments': ''}}]})]
            return []
        if kind == 'response.content_part.added':
            index=self.index(obj,'output_index')
            block=self.blocks[index]
            if block['type']!='message' or block['closed']:raise ValueError('Text part has no open message')
            part=obj['part']
            if part['type'] not in ('output_text','refusal'):raise ValueError('Unsupported native output content')
            return self.text_delta(block,index,self.index(obj,'content_index'),part.get('text',part.get('refusal','')))
        if kind in ('response.output_text.delta', 'response.refusal.delta', 'response.function_call_arguments.delta'):
            index = self.index(obj, 'output_index')
            block = self.blocks[index]
            if block['closed'] or not isinstance(obj['delta'], str):
                raise ValueError('Invalid native delta')
            if kind == 'response.function_call_arguments.delta':
                if block['type'] != 'function_call':
                    raise ValueError('Tool delta has no function item')
                block['arguments'] += obj['delta']
                return [self.chunk({'tool_calls': [{'index': index, 'function': {'arguments': obj['delta']}}]})]
            if block['type'] != 'message':
                raise ValueError('Text delta has no message item')
            return self.text_delta(block,index,self.index(obj,'content_index'),obj['delta'])
        if kind == 'response.output_item.done':
            block = self.blocks[self.index(obj, 'output_index')]
            if block['closed']:
                raise ValueError('Duplicate output item stop')
            final = obj['item']
            if block['type'] == 'function_call':
                if final['arguments'] != block['arguments'] or final['name'] != block['name'] or final['call_id'] != block['call_id']:
                    raise ValueError('Final native tool differs from streamed arguments')
                if not isinstance(strict_json(block['arguments']), dict):
                    raise ValueError('Native tool arguments must be objects')
            elif '\n'.join(p.get('text',p.get('refusal','')) for p in final['content'])!='\n'.join(block['parts'].values()):
                raise ValueError('Final native message differs from streamed parts')
            block['closed'] = True
            return []
        if kind in ('response.completed', 'response.incomplete'):
            from .native import provider_result
            result = provider_result('responses', obj['response'], self.request_id, self.model)
            if obj['response']['status']!=('completed' if kind=='response.completed' else 'incomplete'):
                raise ValueError('Native terminal event does not match its status')
            if any(not b['closed'] for b in self.blocks.values()):
                raise ValueError('Native response stopped before its items completed')
            if self.text != result['choices'][0]['message']['content']:
                raise ValueError('Final native text differs from streamed content')
            calls=[{'id':b['call_id'],'type':'function','function':{'name':b['name'],'arguments':b['arguments']}} for b in self.blocks.values() if b['type']=='function_call']
            if calls!=result['choices'][0]['message'].get('tool_calls',[]):raise ValueError('Final native calls differ from streamed calls')
            self.terminal = True
            return [self.chunk(reason=result['choices'][0]['finish_reason'], usage=result.get('usage'))]
        return []

    def text_delta(self,block,index,content_index,value):
        if not isinstance(value,str):raise ValueError('Invalid native text part')
        key=(index,content_index)
        prefix='\n' if key not in self.parts and self.parts else ''
        self.parts.add(key)
        block['parts'][content_index]=block['parts'].get(content_index,'')+value
        self.text+=prefix+value
        return [self.chunk({'content':prefix+value})] if prefix or value else []


async def upstream_chunks(response, provider, request_id, model):
    state = Upstream(provider, request_id, model)
    async for payload in sse_data(bounded_lines(response)):
        for chunk in state.feed(strict_json(payload)):
            yield chunk
        if state.terminal:
            return
    raise ValueError('Native provider stream ended without its terminal event')


async def upstream_lines(response, provider, request_id, model):
    async for packet in upstream_chunks(response, provider, request_id, model):
        yield 'data: ' + json.dumps(packet, ensure_ascii=False, allow_nan=False)
        yield ''
    yield 'data: [DONE]'
    yield ''


async def iterator_lines(source):
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    pending = ''
    async for chunk in source:
        pending += decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
        while '\n' in pending:
            line, pending = pending.split('\n', 1)
            if len(line.encode()) > MAX_STREAM_LINE_BYTES:
                raise ValueError('Chat SSE line exceeded its limit')
            yield line.rstrip('\r')
        if len(pending.encode()) > MAX_STREAM_LINE_BYTES:
            raise ValueError('Chat SSE line exceeded its limit')
    pending += decoder.decode(b'', final=True)
    if pending:
        yield pending.rstrip('\r')


class ClientEvents:
    def __init__(self, interface, request_id, model):
        self.interface, self.id, self.model = interface, request_id, model
        self.sequence = self.size = 0
        self.blocks = {}
        self.text = ''
        self.calls = {}
        self.reason = 'stop'
        self.usage = {}

    def event(self, kind, **fields):
        value = {'type': kind, **fields}
        if self.interface == 'responses':
            value['sequence_number'] = self.sequence
            self.sequence += 1
        return 'event: ' + kind + '\n' + sse(value)

    def result(self):
        from .native import native_result
        message = {'role': 'assistant', 'content': self.text}
        if self.calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in self.calls.values()]
        return native_result({'choices': [{'message': message, 'finish_reason': self.reason}], 'usage': self.usage}, self.interface, self.id, self.model)

    def start(self):
        result = self.result()
        if self.interface == 'responses':
            return self.event('response.created', response={**result, 'status': 'in_progress', 'output': [], 'usage': None})
        return self.event('message_start', message={**result, 'content': [], 'stop_reason': None,
                          'usage': {'input_tokens': None, 'output_tokens': 0}})

    def open(self, key, item):
        index = len(self.blocks)
        self.blocks[key] = (index, item)
        if self.interface == 'anthropic':
            return [self.event('content_block_start', index=index, content_block=item)]
        result = [self.event('response.output_item.added', output_index=index, item=item)]
        if item['type'] == 'message':
            result.append(self.event('response.content_part.added', item_id=item['id'], output_index=index,
                                     content_index=0, part={'type': 'output_text', 'text': '', 'annotations': []}))
        return result

    def feed(self, packet):
        self.size += len(json.dumps(packet, ensure_ascii=False).encode())
        if self.size > MAX_RESPONSE_BYTES:
            raise ValueError('Native client output exceeded its size limit')
        if packet.get('error'):
            raise ValueError('Upstream stream failed')
        if packet.get('usage'):
            self.usage = packet['usage']
        result = []
        for choice in packet.get('choices', []):
            if choice.get('index', 0) != 0:
                raise ValueError('Only one streaming choice is supported')
            self.reason = choice.get('finish_reason') or self.reason
            delta = choice.get('delta') or {}
            text = delta.get('content') or delta.get('refusal') or ''
            if not isinstance(text, str):
                raise ValueError('Invalid streaming content')
            if text:
                if 'text' not in self.blocks:
                    item = {'type': 'text', 'text': ''} if self.interface == 'anthropic' else {'type': 'message', 'id': 'msg_' + self.id, 'role': 'assistant', 'status': 'in_progress', 'content': []}
                    result += self.open('text', item)
                index, item = self.blocks['text']
                self.text += text
                result.append(self.event('content_block_delta', index=index, delta={'type': 'text_delta', 'text': text}) if self.interface == 'anthropic' else
                              self.event('response.output_text.delta', item_id=item['id'], output_index=index, content_index=0, delta=text, logprobs=[]))
            for tool in delta.get('tool_calls') or []:
                key = 'tool_' + str(count(tool['index']))
                if len(self.calls) >= 128 and key not in self.calls:
                    raise ValueError('Too many streamed tool calls')
                call = self.calls.setdefault(key, {'id': '', 'name': '', 'arguments': ''})
                function = tool.get('function') or {}
                if key in self.blocks and (tool.get('id') or function.get('name')):
                    raise ValueError('Tool identity changed after its start')
                call['id'] += tool.get('id') or ''
                call['name'] += function.get('name') or ''
                fragment = function.get('arguments') or ''
                call['arguments'] += fragment
                if key not in self.blocks:
                    if not call['id'] or not call['name']:
                        continue
                    item = {'type': 'tool_use', 'id': call['id'], 'name': call['name'], 'input': {}} if self.interface == 'anthropic' else {'type': 'function_call', 'id': 'fc_' + self.id + '_' + str(len(self.calls)), 'call_id': call['id'], 'name': call['name'], 'arguments': '', 'status': 'in_progress'}
                    result += self.open(key, item)
                    fragment = call['arguments']
                if fragment:
                    index, item = self.blocks[key]
                    result.append(self.event('content_block_delta', index=index, delta={'type': 'input_json_delta', 'partial_json': fragment}) if self.interface == 'anthropic' else
                                  self.event('response.function_call_arguments.delta', item_id=item['id'], output_index=index, delta=fragment))
        return result

    def finish(self):
        for call in self.calls.values():
            if not call['id'] or not call['name'] or not isinstance(strict_json(call['arguments']), dict):
                raise ValueError('Incomplete native tool call')
        result = []
        final = self.result()
        for key, (index, item) in self.blocks.items():
            if self.interface == 'anthropic':
                result.append(self.event('content_block_stop', index=index))
                continue
            if key == 'text':
                part = {'type': 'output_text', 'text': self.text, 'annotations': []}
                result += [self.event('response.output_text.done', item_id=item['id'], output_index=index, content_index=0, text=self.text, logprobs=[]),
                           self.event('response.content_part.done', item_id=item['id'], output_index=index, content_index=0, part=part)]
                completed = {**item, 'status': 'completed', 'content': [part]}
            else:
                arguments = self.calls[key]['arguments']
                result.append(self.event('response.function_call_arguments.done', item_id=item['id'], output_index=index, arguments=arguments))
                completed = {**item, 'status': 'completed', 'arguments': arguments}
            result.append(self.event('response.output_item.done', output_index=index, item=completed))
        if self.interface == 'responses':
            # Preserve the same item identities/order as the preceding deltas.
            final['output'] = [{**item, 'status': 'completed', **({'content': [{'type': 'output_text', 'text': self.text, 'annotations': []}]} if key == 'text' else {'arguments': self.calls[key]['arguments']})} for key, (_, item) in self.blocks.items()]
            kind = 'response.incomplete' if final['status'] == 'incomplete' else 'response.completed'
            result.append(self.event(kind, response=final))
        else:
            result += [self.event('message_delta', delta={'stop_reason': final['stop_reason'], 'stop_sequence': None}, usage=final['usage']), self.event('message_stop')]
        return result


async def client_events(source, interface, request_id, model, on_error=None):
    state = ClientEvents(interface, request_id, model)
    failed = False
    try:
        yield state.start()
        async for payload in sse_data(iterator_lines(source)):
            if payload == '[DONE]':
                for event in state.finish():
                    yield event
                return
            for event in state.feed(strict_json(payload)):
                yield event
        raise ValueError('Chat stream ended without its terminal event')
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        failed = True
        yield state.event('error', error={'type': 'upstream_stream_error', 'message': 'The model stream did not complete', 'request_id': request_id})
    finally:
        if hasattr(source, 'aclose'):
            await source.aclose()
        if failed and on_error:
            on_error()
