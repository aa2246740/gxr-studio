// macOS 系统自带 ImageIO 能不能把文件认成"带增益图的 HDR 图"。只读输入文件。来源：research/hdr_gainmap/check_imageio.swift（原样）。
// 用法：check_imageio a.jpg [b.jpg …]
//   每个文件打印一行 JSON：ISO 增益图辅助数据、Apple HDR 增益图辅助数据、按 HDR 解码后的内容余量（headroom）。
// 用法：check_imageio --compare 原.JPG 新_HDR.JPG 输出.f32
//   用系统解码器比较两者的 SDR 像素（8 位 sRGB 逐字节），并把新文件 SDR / HDR 解码的线性亮度
//   按 4×4 块平均写成 float32（先 SDR 后 HDR，各 ceil(H/4)×ceil(W/4)），供 selfcheck.py 对照增益。
import Foundation
import ImageIO
import CoreGraphics

func auxInfo(_ src: CGImageSource, _ type: CFString) -> [String: Any]? {
    guard let info = CGImageSourceCopyAuxiliaryDataInfoAtIndex(src, 0, type) as? [String: Any] else { return nil }
    var out: [String: Any] = [:]
    if let desc = info[kCGImageAuxiliaryDataInfoDataDescription as String] as? [String: Any] {
        out["width"] = desc["Width"]
        out["height"] = desc["Height"]
        out["bytesPerRow"] = desc["BytesPerRow"]
        out["pixelFormat"] = desc["PixelFormat"]
    }
    if let data = info[kCGImageAuxiliaryDataInfoData as String] as? Data { out["dataBytes"] = data.count }
    if let meta = info[kCGImageAuxiliaryDataInfoMetadata as String] {
        let m = meta as! CGImageMetadata
        var tags: [String] = []
        if let arr = CGImageMetadataCopyTags(m) as? [CGImageMetadataTag] {
            for t in arr {
                let prefix = CGImageMetadataTagCopyPrefix(t).map { $0 as String } ?? "?"
                let name = CGImageMetadataTagCopyName(t).map { $0 as String } ?? "?"
                let value = CGImageMetadataTagCopyValue(t).map { "\($0)" } ?? ""
                tags.append("\(prefix):\(name)=\(value)")
            }
        }
        out["metadata"] = tags
    }
    return out
}

func render(_ img: CGImage, space: CGColorSpace, float: Bool) -> [UInt8] {
    let w = img.width, h = img.height
    let bpc = float ? 32 : 8
    let rowBytes = w * 4 * (bpc / 8)
    var buf = [UInt8](repeating: 0, count: rowBytes * h)
    let info: UInt32 = float
        ? CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.floatComponents.rawValue | CGBitmapInfo.byteOrder32Little.rawValue
        : CGImageAlphaInfo.premultipliedLast.rawValue
    buf.withUnsafeMutableBytes { p in
        let ctx = CGContext(data: p.baseAddress, width: w, height: h, bitsPerComponent: bpc, bytesPerRow: rowBytes,
                            space: space, bitmapInfo: info)!
        ctx.interpolationQuality = .none
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
    }
    return buf
}

func blockLuma(_ buf: [UInt8], _ w: Int, _ h: Int) -> [Float] {
    let gw = (w + 3) / 4, gh = (h + 3) / 4
    var out = [Float](repeating: 0, count: gw * gh)
    var cnt = [Float](repeating: 0, count: gw * gh)
    buf.withUnsafeBytes { raw in
        let f = raw.bindMemory(to: Float.self)
        for y in 0..<h {
            for x in 0..<w {
                let i = (y * w + x) * 4
                let l = 0.2126 * f[i] + 0.7152 * f[i + 1] + 0.0722 * f[i + 2]
                let g = (y / 4) * gw + x / 4
                out[g] += l
                cnt[g] += 1
            }
        }
    }
    for i in 0..<out.count { out[i] /= cnt[i] }
    return out
}

let args = Array(CommandLine.arguments.dropFirst())
if args.first == "--compare" && args.count == 4 {
    let srcA = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[1]) as CFURL, nil)!
    let srcB = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[2]) as CFURL, nil)!
    let a = CGImageSourceCreateImageAtIndex(srcA, 0, nil)!
    let b = CGImageSourceCreateImageAtIndex(srcB, 0, nil)!
    let srgb = CGColorSpace(name: CGColorSpace.sRGB)!
    let ra = render(a, space: srgb, float: false), rb = render(b, space: srgb, float: false)
    var nDiff = 0, maxDiff = 0
    if ra.count == rb.count {
        for i in 0..<ra.count where ra[i] != rb[i] {
            nDiff += 1
            maxDiff = max(maxDiff, abs(Int(ra[i]) - Int(rb[i])))
        }
    }
    let same = a.width == b.width && a.height == b.height && nDiff == 0
    let lin = CGColorSpace(name: CGColorSpace.extendedLinearSRGB)!
    let opts = [kCGImageSourceDecodeRequest: kCGImageSourceDecodeToHDR] as CFDictionary
    let hdr = CGImageSourceCreateImageAtIndex(srcB, 0, opts)!
    let ls = blockLuma(render(b, space: lin, float: true), b.width, b.height)
    let lh = blockLuma(render(hdr, space: lin, float: true), hdr.width, hdr.height)
    var data = Data()
    for v in ls + lh { var x = v; data.append(Data(bytes: &x, count: 4)) }
    try! data.write(to: URL(fileURLWithPath: args[3]))
    let rec: [String: Any] = ["file": (args[2] as NSString).lastPathComponent, "sdrPixelsIdenticalImageIO": same,
                              "sdrDiffBytes": nDiff, "sdrMaxAbsDiff": maxDiff, "sdrBytesTotal": ra.count,
                              "hdrSize": [hdr.width, hdr.height], "hdrHeadroom": hdr.contentHeadroom]
    print(String(data: try! JSONSerialization.data(withJSONObject: rec, options: [.sortedKeys]), encoding: .utf8)!)
    exit(0)
}

for path in args {
    var rec: [String: Any] = ["file": (path as NSString).lastPathComponent]
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else {
        rec["error"] = "cannot open"
        print(rec)
        continue
    }
    rec["imageCount"] = CGImageSourceGetCount(src)
    rec["isoGainMap"] = auxInfo(src, kCGImageAuxiliaryDataTypeISOGainMap) ?? NSNull()
    rec["appleHDRGainMap"] = auxInfo(src, kCGImageAuxiliaryDataTypeHDRGainMap) ?? NSNull()
    if let sdr = CGImageSourceCreateImageAtIndex(src, 0, nil) {
        rec["sdrHeadroom"] = sdr.contentHeadroom
        rec["sdrSize"] = [sdr.width, sdr.height]
    }
    let opts = [kCGImageSourceDecodeRequest: kCGImageSourceDecodeToHDR] as CFDictionary
    if let hdr = CGImageSourceCreateImageAtIndex(src, 0, opts) {
        rec["hdrHeadroom"] = hdr.contentHeadroom
        rec["hdrBitsPerComponent"] = hdr.bitsPerComponent
        rec["hdrColorSpace"] = hdr.colorSpace?.name.map { $0 as String } ?? "?"
    }
    let json = try! JSONSerialization.data(withJSONObject: rec, options: [.sortedKeys])
    print(String(data: json, encoding: .utf8)!)
}
