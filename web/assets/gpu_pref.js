// 显卡实时预览的本机选择：服务器开关（gpu_preview）打开时默认用；「设置 → 预览」可以只在这台电脑上关掉。
// 存在浏览器的 localStorage 里（键 gxr-studio.gpu-preview，值 on / off），不写服务器。
import { emit } from './state.js';

const GPU_KEY = 'gxr-studio.gpu-preview';

export function gpuPreference() {
  try {
    return localStorage.getItem(GPU_KEY) !== 'off';
  } catch {
    return true;
  }
}

export function setGpuPreference(on) {
  try {
    localStorage.setItem(GPU_KEY, on ? 'on' : 'off');
  } catch {
    // 隐私模式下存不住：这次打开期间照样生效
  }
  emit('gpu-pref-changed', on);
}
