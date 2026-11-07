// 直方图：在成片纹理上按网格取样，数红、绿、蓝、亮度各 256 档，再数"暗部压死""高光溢出"的点。
// 取样点 = 矩形 (x0, y0) 起，横竖每隔 stride 个像素一个，共 nx × ny 个；取法、亮度公式和溢出判定
// 与 web/assets/histogram_math.js 的 countRGBA 完全一致（tests/test_histogram_gpu.py 用 numpy 逐个核对）。
// 每个工作组先在组内数一份，最后再并进全局，全局原子操作不会成为瓶颈。
struct H {
  grid: vec4u,   // x0、y0、nx、ny
  rule: vec4u,   // 间隔、暗部上限、高光下限、0
};
@group(0) @binding(0) var<uniform> u: H;
@group(0) @binding(1) var src: texture_2d<f32>;
@group(0) @binding(2) var<storage, read_write> total: array<atomic<u32>, 1026>;
var<workgroup> part: array<atomic<u32>, 1026>;

// 派发 8 × 8 个工作组、每组 16 × 16 个线程 = 128 × 128 个线程，每个线程隔 128 个格点取一个
const THREADS: u32 = 128u;

@compute @workgroup_size(16, 16) fn main(
  @builtin(local_invocation_index) li: u32,
  @builtin(local_invocation_id) lid: vec3u,
  @builtin(workgroup_id) wg: vec3u,
) {
  for (var i = li; i < 1026u; i += 256u) { atomicStore(&part[i], 0u); }
  workgroupBarrier();
  let stride = u.rule.x;
  for (var j = wg.y * 16u + lid.y; j < u.grid.w; j += THREADS) {
    for (var i = wg.x * 16u + lid.x; i < u.grid.z; i += THREADS) {
      let p = vec2i(vec2u(u.grid.x + i * stride, u.grid.y + j * stride));
      let c = vec3u(round(textureLoad(src, p, 0).xyz * 255.0));
      atomicAdd(&part[c.x], 1u);
      atomicAdd(&part[256u + c.y], 1u);
      atomicAdd(&part[512u + c.z], 1u);
      atomicAdd(&part[768u + (2126u * c.x + 7152u * c.y + 722u * c.z + 5000u) / 10000u], 1u);
      if (max(c.x, max(c.y, c.z)) <= u.rule.y) { atomicAdd(&part[1024], 1u); }
      if (min(c.x, min(c.y, c.z)) >= u.rule.z) { atomicAdd(&part[1025], 1u); }
    }
  }
  workgroupBarrier();
  for (var i = li; i < 1026u; i += 256u) {
    let n = atomicLoad(&part[i]);
    if (n != 0u) { atomicAdd(&total[i], n); }
  }
}
