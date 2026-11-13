import { boot } from './ui/devshell';

const pageErrors: string[] = [];
window.addEventListener('error', (event) => {
  pageErrors.push(event.message || '页面出错');
});
window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason;
  pageErrors.push(reason instanceof Error ? reason.message : String(reason));
});

const root = document.querySelector('#app');
if (root instanceof HTMLElement) {
  boot(root, pageErrors).catch((err: unknown) => {
    pageErrors.push(err instanceof Error ? err.message : String(err));
  });
}
