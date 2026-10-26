import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { parse, compileTemplate } from '@vue/compiler-sfc'

const root = fileURLToPath(new URL('../src/', import.meta.url))
const router = fs.readFileSync(path.join(root, 'router/index.js'), 'utf8')
const pages = new Set([...router.matchAll(/import\(['"]([^'"]+\.vue)['"]\)/g)]
  .map(match => path.resolve(root, 'router', match[1])))
pages.add(path.join(root, 'views/Login.vue'))

// 路由退出动画必须落到一个真实元素上；检查编译后的根节点，避免把 v-if / v-else 误判为多根。
function checkRoot(filename, visited = new Set()) {
  assert.ok(!visited.has(filename), `根组件存在循环引用：${filename}`)
  const nextVisited = new Set([...visited, filename])
  const { descriptor } = parse(fs.readFileSync(filename, 'utf8'), { filename })
  assert.ok(descriptor.template, `页面缺少模板：${filename}`)
  const compiled = compileTemplate({ source: descriptor.template.content, filename, id: filename })
  assert.deepEqual(compiled.errors, [], `页面模板编译失败：${filename}`)

  function inspect(node) {
    if (node.type === 9) {
      assert.ok(!node.branches.at(-1).condition, `条件根节点缺少兜底元素：${filename}`)
      for (const branch of node.branches) {
        assert.equal(branch.children.length, 1, `条件分支必须只有一个根节点：${filename}`)
        inspect(branch.children[0].codegenNode)
      }
      return
    }
    assert.equal(node.type, 13, `动画根节点必须是元素或单根组件：${filename}`)
    if (typeof node.tag === 'string' && /^"[a-z][\w-]*"$/.test(node.tag)) return
    const name = typeof node.tag === 'string' && node.tag.replace(/^_component_/, '')
    const source = [descriptor.script?.content, descriptor.scriptSetup?.content].filter(Boolean).join('\n')
    const imports = [...source.matchAll(/import\s+(\w+)\s+from\s+['"]([^'"]+\.vue)['"]/g)]
    const imported = imports.find(match => match[1] === name)
    assert.ok(imported, `动画根节点不是可核对的真实元素或本地组件：${filename}，${String(node.tag)}`)
    checkRoot(path.resolve(path.dirname(filename), imported[2]), nextVisited)
  }

  inspect(compiled.ast.codegenNode)
}

for (const filename of pages) {
  test(`路由页面及所有根分支均支持退出动画：${path.basename(filename)}`, () => checkRoot(filename))
}
