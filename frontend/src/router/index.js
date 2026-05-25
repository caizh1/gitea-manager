import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/dashboard', component: () => import('../views/Dashboard.vue') },
  { path: '/servers/:id', component: () => import('../views/ServerDetail.vue') },
  { path: '/servers', component: () => import('../views/Servers.vue') },
  { path: '/backups', component: () => import('../views/Backups.vue') },
  { path: '/restore', component: () => import('../views/Restore.vue') },
  { path: '/mirrors', component: () => import('../views/Mirrors.vue') },
  { path: '/statistics/authors', component: () => import('../views/AuthorList.vue') },
  { path: '/statistics/authors/:name', component: () => import('../views/AuthorDetail.vue') },
  { path: '/statistics', component: () => import('../views/Statistics.vue') },
  { path: '/settings', component: () => import('../views/Settings.vue') },
  { path: '/schedules', component: () => import('../views/Schedule.vue') },
  { path: '/', redirect: '/dashboard' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
