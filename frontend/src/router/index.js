import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/access', meta: { capability: 'access' }, component: () => import('../views/AccessManagement.vue') },
  { path: '/management-setup', meta: { capability: 'management' }, component: () => import('../views/ManagementSetup.vue') },
  { path: '/dashboard', meta: { capability: 'dashboard' }, component: () => import('../views/Dashboard.vue') },
  { path: '/servers/:id', meta: { capability: 'servers' }, component: () => import('../views/ServerDetail.vue') },
  { path: '/servers', meta: { capability: 'servers' }, component: () => import('../views/Servers.vue') },
  { path: '/backups', meta: { capability: 'backups' }, component: () => import('../views/Backups.vue') },
  { path: '/restore', meta: { capability: 'restore' }, component: () => import('../views/Restore.vue') },
  { path: '/mirrors', meta: { capability: 'mirrors' }, component: () => import('../views/Mirrors.vue') },
  { path: '/repository-policies', meta: { capability: 'repository_policies' }, component: () => import('../views/RepositoryPolicies.vue') },
  { path: '/approval-rules', meta: { capability: 'approval_rules' }, component: () => import('../views/ApprovalRules.vue') },
  { path: '/ai-reviews', meta: { capability: 'ai_reviews' }, component: () => import('../views/AIReviews.vue') },
  { path: '/commit-gates', meta: { capability: 'commit_gates' }, component: () => import('../views/CommitGates.vue') },
  { path: '/statistics/authors', meta: { capability: 'statistics' }, component: () => import('../views/AuthorList.vue') },
  { path: '/statistics/authors/:name', meta: { capability: 'statistics' }, component: () => import('../views/AuthorDetail.vue') },
  { path: '/statistics', meta: { capability: 'statistics' }, component: () => import('../views/Statistics.vue') },
  { path: '/settings', meta: { capability: 'settings' }, component: () => import('../views/Settings.vue') },
  { path: '/schedules', meta: { capability: 'schedules' }, component: () => import('../views/Schedule.vue') },
  { path: '/', redirect: '/dashboard' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
