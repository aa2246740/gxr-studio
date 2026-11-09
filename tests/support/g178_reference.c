/* Compare with the reviewed camera parsers and integer engine, compiled locally. */
#include "g152_reference.c"

int studio_full_record(const uint8_t *raw, size_t length, unsigned unit,
                       unsigned position, const int16_t levels[10], uint8_t out[552])
{
    Gxr2View view;
    KvDataView data;
    KvRequest request;
    KvWork work;
    KvStats stats;
    UvLevels values;
    uint8_t stock[552], base[552];
    unsigned i, index;
    const uint8_t *entry;
    int result = view_of(raw, length, unit, &view);
    if (result) return result;
    if (position < 1 || position > 23) return -4;
    if (position <= 4) {
        if (gxr2_expand(&view, unit, position, base)) return -5;
    } else {
        index = 3 * (position - 5) + unit;
        entry = fx_entry(&view, index);
        if (!entry || !(fx_unit_mask(&view, entry) & (1u << unit)) || fx_expand(&view, index, base)) return -6;
    }
    if (gxr2_expand(&view, unit, 4, stock)) return -7;
    if (kv_data_open(raw + length - KV_DATA_BYTES, KV_DATA_BYTES, &data)) return -8;
    for (i = 0; i < 10; ++i) values.v[i] = levels[i];
    if (kv_data_request(&data, unit, base[0], &values, &request)) return -9;
    return kv_apply_work(stock, base, &request, out, &stats, &work);
}
