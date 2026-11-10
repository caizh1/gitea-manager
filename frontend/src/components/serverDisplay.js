// 使用已登记的角色和连接地址识别实例，不根据容器名称猜测主备。
export const serverRoleLabel = role => ({ primary: '主服务器', backup: '备服务器', archive: '归档服务器' }[role] || '角色未设置')

export function serverAddress(server) {
  if (server.gitea_url || server.url) return server.gitea_url || server.url
  if (!server.host) return ''
  const host = server.host.includes(':') && !server.host.startsWith('[') ? `[${server.host}]` : server.host
  return server.gitea_port ? `${host}:${server.gitea_port}` : host
}

export function serverLabel(server) {
  if (!server) return '请选择目标实例'
  return [serverRoleLabel(server.role), server.name || `实例 #${server.id}`, serverAddress(server)].filter(Boolean).join(' · ')
}
