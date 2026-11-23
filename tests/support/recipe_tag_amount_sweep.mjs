// 验证手机端 tagAmountLevels / 封存目录代数表与桌面 _catalog_recipe / PROFILES 一致。
import { existsSync, readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { pathToFileURL } from 'node:url';

registerHooks({
  resolve(spec, ctx, next) {
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
});

const [mode, mobileSrcDir, payloadPath] = process.argv.slice(2);
const modUrl = (rel) => pathToFileURL(`${mobileSrcDir}/${rel}`).href;
const hasRecipeTagsModule = existsSync(`${mobileSrcDir}/lib/recipe_tags.ts`);
const tagMod = await import(modUrl(hasRecipeTagsModule ? 'lib/recipe_tags.ts' : 'lib/halfframe.ts'));
const recMod = await import(modUrl('lib/recipes.ts'));

if (mode === 'sweep') {
  const fx = JSON.parse(readFileSync(payloadPath, 'utf8'));
  const dat = { generation: fx.generation, listing: { units: fx.listing } };
  const out = {};
  for (const identity of fx.catalogs) {
    for (const unit of fx.units) {
      for (let pos = 1; pos <= fx.maxPos; pos += 1) {
        const tagText = `GXR1 ${identity} ${unit} ${String(pos).padStart(2, '0')} A99999999-`;
        const parsed = tagMod.parseRecipeTag(tagText);
        out[`${identity}|${unit}|${pos}`] = recMod.tagAmountLevels(parsed, dat);
      }
    }
  }
  process.stdout.write(`${JSON.stringify(out)}\n`);
} else if (mode === 'maps') {
  const fx = JSON.parse(readFileSync(payloadPath, 'utf8'));
  const unregistered = {};
  for (const gen of fx.unregisteredGenerations) {
    const parsed = tagMod.parseRecipeTag(fx.probeTag);
    unregistered[String(gen)] = recMod.tagAmountLevels(parsed, { generation: gen, listing: { units: fx.listing } });
  }
  const descriptors = tagMod.CATALOG_DESCRIPTORS
    ? Object.fromEntries(
        Object.entries(tagMod.CATALOG_DESCRIPTORS).map(([k, v]) => [k, v.map(([name, mask]) => [name, mask])]),
      )
    : null;
  process.stdout.write(
    `${JSON.stringify({
      hasRecipeTagsModule,
      tagCatalogGenerations: tagMod.TAG_CATALOG_GENERATIONS ?? null,
      datLibraryGenerations: tagMod.DAT_LIBRARY_GENERATIONS ?? null,
      catalogDescriptors: descriptors,
      unregistered,
    })}\n`,
  );
} else {
  throw new Error(`unknown mode: ${mode}`);
}
