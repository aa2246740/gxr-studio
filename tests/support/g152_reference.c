/* Host comparison using the camera's unchanged parsers and integer core. */
#include "fx_pool.h"
#include "ui_v2_data.h"
#include "kv2/kv_ui.h"

static int view_of(const uint8_t *raw, size_t length, unsigned unit, Gxr2View *view)
{
    Gxr2Trust trust = {0};
    Gxr2View gate;
    unsigned i;
    if (length < 256 || unit >= 3) return -1;
    ig_copy(trust.body_sha256, raw + 48, 32);
    ig_copy(trust.resident_build_id, raw + 80, 32);
    ig_copy(trust.resident_abi_id, raw + 212, 16);
    for (i = 0; i < 3; ++i) {
        const uint8_t *d = raw + be32(raw + 156) + 64 * i;
        ig_copy(trust.modules[i].unit.name, d, 8);
        trust.modules[i].unit.main_version = be16(d + 8);
        trust.modules[i].unit.cmp_version = be16(d + 10);
        trust.modules[i].stock_crc32 = be32(d + 32);
        ig_copy(trust.modules[i].stock_sha256, d + 32, 32);
    }
    if (gxr2_gate_validate(raw, length, &trust, &trust.modules[unit].unit, 0, &gate)) return -2;
    if (gxr2_semantic_validate(&gate, &trust, view) || !uv_validate(view)) return -3;
    return 0;
}

int studio_validate(const uint8_t *raw, size_t length, unsigned unit)
{
    Gxr2View view;
    return view_of(raw, length, unit, &view);
}

int studio_builtin(const uint8_t *raw, size_t length, unsigned unit, unsigned slot,
                   const int16_t levels[10], uint8_t out[552])
{
    Gxr2View view;
    KvDataView data;
    KvRequest request;
    KvWork work;
    KvStats stats;
    UvLevels values;
    uint8_t stock[552], base[552];
    unsigned i;
    int result = view_of(raw, length, unit, &view);
    if (result) return result;
    if (gxr2_expand(&view, unit, slot, base) || gxr2_expand(&view, unit, 4, stock)) return -5;
    if (kv_data_open(raw + view.sections[6].offset + 3392, KV_DATA_BYTES, &data)) return -6;
    for (i = 0; i < 10; ++i) values.v[i] = levels[i];
    if (kv_data_request(&data, unit, slot, &values, &request)) return -7;
    return kv_apply_work(stock, base, &request, out, &stats, &work);
}

int studio_resolve(const uint8_t *raw, size_t length, unsigned unit, unsigned index,
                   const int16_t levels[10], uint8_t out[552])
{
    Gxr2View view;
    KvDataView data;
    KvRequest request;
    KvWork work;
    KvStats stats;
    UvLevels values;
    uint8_t stock[552], base[552];
    unsigned i;
    const uint8_t *entry;
    int result = view_of(raw, length, unit, &view);
    if (result) return result;
    entry = fx_entry(&view, index);
    if (!entry || !(entry[18] & (1u << unit))) return -4;
    if (fx_expand(&view, index, base) || gxr2_expand(&view, unit, 4, stock)) return -5;
    if (kv_data_open(raw + view.sections[6].offset + 3392, KV_DATA_BYTES, &data)) return -6;
    for (i = 0; i < 10; ++i) values.v[i] = levels[i];
    if (kv_data_request(&data, unit, base[0], &values, &request)) return -7;
    return kv_apply_work(stock, base, &request, out, &stats, &work);
}

int studio_user(const uint8_t *raw, size_t length, unsigned unit, unsigned index, uint8_t out[552])
{
    Gxr2View view;
    KvDataView data;
    KvRequest request;
    KvWork work;
    KvStats stats;
    UvLevels values;
    uint8_t stock[552], base[552];
    unsigned slot;
    int result = view_of(raw, length, unit, &view);
    if (result) return result;
    if (kv_ui_legacy_record(uv_user(&view, index), 24, &slot, &values)) return -4;
    if (gxr2_expand(&view, unit, slot, base) || gxr2_expand(&view, unit, 4, stock)) return -5;
    if (kv_data_open(raw + view.sections[6].offset + 3392, KV_DATA_BYTES, &data)) return -6;
    if (kv_data_request(&data, unit, slot, &values, &request)) return -7;
    return kv_apply_work(stock, base, &request, out, &stats, &work);
}
