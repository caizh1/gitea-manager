import { reactive } from 'vue'
import { accessState } from './accessState.js'

// 仅在当前授权会话保留仓库 ID；跨来源、返回工作台后也不会丢失选择。
const selection = reactive({ generation: null, ids: [] })
export function useSvnSelection() {
  if (selection.generation !== accessState.generation) {
    selection.generation = accessState.generation
    selection.ids = []
  }
  return selection
}
