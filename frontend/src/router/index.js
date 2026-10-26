import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/account/security', meta: { account: true }, component: () => import('../views/AccountSecurity.vue') },
  { path: '/git-migrations', meta: { capability: 'git_migrations', platformOnly: true }, component: () => import('../views/GitMigrations.vue') },
  ...[
    ['', 'workbench'], ['/sources', 'sources'], ['/runs', 'runs'],
    ['/plans/new', 'plan'], ['/plans/:planId/edit', 'plan'],
    ['/runs/:runId', 'run'], ['/preflights/:preflightId', 'preflight'],
  ].map(([path, svnPage]) => ({ path: '/svn-migrations' + path,
    meta: { capability: 'svn_migrations', platformOnly: true, svnPage },
    component: () => import('../views/SvnMigrations.vue') })),
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
  { path: '/ai-reviews/repositories', meta: { capability: 'ai_reviews', reviewSection: 'repositories' }, component: () => import('../views/AIReviews.vue') },
  { path: '/ai-reviews/dataset', meta: { capability: 'ai_reviews', reviewSection: 'dataset' }, component: () => import('../views/AIReviews.vue') },
  { path: '/ai-reviews/settings', meta: { capability: 'ai_reviews', reviewSection: 'config', platformOnly: true }, component: () => import('../views/AIReviews.vue') },
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
