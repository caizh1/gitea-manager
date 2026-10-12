// 只由执行器注入到独立配置目录，不加载待审仓库中的工具。
import { tool } from '@opencode-ai/plugin';
import schema from '../schema.json';

function convert(s) {
  const z = tool.schema;
  if (s.enum) return z.enum(s.enum);
  if (s.type === 'string') return z.string();
  if (s.type === 'integer') return z.number().int().min(s.minimum ?? 0);
  if (s.type === 'array') return z.array(convert(s.items));
  if (s.type === 'object') return z.object(Object.fromEntries(Object.entries(s.properties).map(([k, v]) => [k, convert(v)]))).strict();
  throw new Error('不支持的结果结构');
}

export default tool({
  description: '审查结束后必须调用且只调用一次，提交完整结构化结果；随后结束回答。',
  args: Object.fromEntries(Object.entries(schema.properties).map(([k, v]) => [k, convert(v)])),
  async execute() { return '管理器已接收结构化结果。'; },
});
