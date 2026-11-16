// 边框与时间戳的六套字体及其开源许可（经 Vite `?url` 直接从 `src/gxr_studio/assets/fonts/` 打包，不复制进 `mobile/`）。

import atkinsonUrl from '../../../src/gxr_studio/assets/fonts/AtkinsonHyperlegible-Regular.ttf?url';
import interUrl from '../../../src/gxr_studio/assets/fonts/Inter-Regular.ttf?url';
import interSbUrl from '../../../src/gxr_studio/assets/fonts/Inter-SemiBold.ttf?url';
import courierUrl from '../../../src/gxr_studio/assets/fonts/CourierPrime-Regular.ttf?url';
import patrickUrl from '../../../src/gxr_studio/assets/fonts/PatrickHand-Regular.ttf?url';
import dsegUrl from '../../../src/gxr_studio/assets/fonts/DSEG7Classic-Regular.ttf?url';

import atkinsonLic from '../../../src/gxr_studio/assets/fonts/AtkinsonHyperlegible-OFL.txt?url';
import interLic from '../../../src/gxr_studio/assets/fonts/Inter-OFL.txt?url';
import interSbLic from '../../../src/gxr_studio/assets/fonts/Inter-SemiBold-OFL.txt?url';
import courierLic from '../../../src/gxr_studio/assets/fonts/CourierPrime-OFL.txt?url';
import patrickLic from '../../../src/gxr_studio/assets/fonts/PatrickHand-OFL.txt?url';
import dsegLic from '../../../src/gxr_studio/assets/fonts/DSEG-LICENSE.txt?url';

import type { FontKey } from './fonts';

export const FONT_URLS: Record<FontKey, string> = {
  atkinson: atkinsonUrl,
  inter: interUrl,
  inter_sb: interSbUrl,
  courier: courierUrl,
  patrick: patrickUrl,
  dseg: dsegUrl,
};

export const FONT_LICENCES: Record<FontKey, string> = {
  atkinson: atkinsonLic,
  inter: interLic,
  inter_sb: interSbLic,
  courier: courierLic,
  patrick: patrickLic,
  dseg: dsegLic,
};

export const FONT_LICENCE_LABELS: Array<[FontKey, string]> = [
  ['atkinson', 'Atkinson Hyperlegible (OFL)'],
  ['inter', 'Inter Regular (OFL)'],
  ['inter_sb', 'Inter SemiBold (OFL)'],
  ['courier', 'Courier Prime (OFL)'],
  ['patrick', 'Patrick Hand (OFL)'],
  ['dseg', 'DSEG7 Classic (SIL OFL)'],
];
