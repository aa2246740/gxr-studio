// iPad 底部常驻双条容器（方案 A）：上方配方/LUT 卡片横条（带左侧纵向「配方 / LUT」切换，所有工具下常驻），下方单行胶片条。

import { button, el, icon } from './dom';
import { FilmstripView } from './filmstrip';
import type { AppApi } from './model';

const CHEV_DOWN = 'M6 9l6 6 6-6';

export interface IpadDockHooks {
  onSubChange(sub: 'recipe' | 'lut'): void;
  onRecipeFilter(): void;
}

export class IpadDock {
  private readonly root: HTMLElement;
  private readonly rstrip: HTMLElement;
  private readonly stripHost: HTMLElement;
  private readonly recipeBtn: HTMLButtonElement;
  private readonly recipeChev: SVGSVGElement;
  private readonly lutBtn: HTMLButtonElement;
  private readonly filmstrip: FilmstripView;
  private sub: 'recipe' | 'lut' = 'recipe';

  constructor(api: AppApi, currentPhotoId: string, hooks: IpadDockHooks) {
    this.root = el('div', 'ipad-dock');
    this.root.dataset.ipadDock = '1';

    this.rstrip = el('div', 'ipad-recipe-strip rstrip');
    this.rstrip.dataset.ipadRecipeStrip = '1';

    const vseg = el('div', 'vseg');
    vseg.setAttribute('role', 'tablist');
    vseg.setAttribute('aria-label', '配方与 LUT');

    this.recipeBtn = button('vseg-btn on', '配方');
    this.recipeBtn.dataset.mode = 'recipe';
    this.recipeBtn.dataset.act = 'recipe-filter';
    this.recipeBtn.setAttribute('role', 'tab');
    this.recipeBtn.setAttribute('aria-selected', 'true');
    this.recipeChev = icon(CHEV_DOWN);
    this.recipeBtn.append(this.recipeChev);
    this.recipeBtn.addEventListener('click', () => {
      if (this.sub === 'recipe') hooks.onRecipeFilter();
      else hooks.onSubChange('recipe');
    });

    this.lutBtn = button('vseg-btn', 'LUT');
    this.lutBtn.dataset.mode = 'lut';
    this.lutBtn.setAttribute('role', 'tab');
    this.lutBtn.setAttribute('aria-selected', 'false');
    this.lutBtn.addEventListener('click', () => {
      if (this.sub !== 'lut') hooks.onSubChange('lut');
    });

    vseg.append(this.recipeBtn, this.lutBtn);
    this.stripHost = el('div', 'ipad-strip-host');
    this.rstrip.append(vseg, this.stripHost);

    this.filmstrip = new FilmstripView(api, currentPhotoId);
    this.root.append(this.rstrip, this.filmstrip.el());
  }

  el(): HTMLElement {
    return this.root;
  }

  stripHostEl(): HTMLElement {
    return this.stripHost;
  }

  syncSub(sub: 'recipe' | 'lut', lutActive: boolean): void {
    this.sub = sub;
    const onRecipe = sub === 'recipe';
    this.recipeBtn.classList.toggle('on', onRecipe);
    this.recipeBtn.setAttribute('aria-selected', onRecipe ? 'true' : 'false');
    this.recipeChev.style.display = onRecipe ? '' : 'none';
    if (onRecipe) this.recipeBtn.dataset.act = 'recipe-filter';
    else delete this.recipeBtn.dataset.act;

    this.lutBtn.classList.toggle('on', !onRecipe);
    this.lutBtn.setAttribute('aria-selected', !onRecipe ? 'true' : 'false');
    this.lutBtn.classList.toggle('dot', lutActive);
  }

  /** iPad 版式才画胶片条；手机上这一块整个隐藏，也不建缩略图。 */
  setActive(on: boolean): void {
    this.filmstrip.setActive(on);
  }

  destroy(): void {
    this.filmstrip.destroy();
  }
}
