// Draws the disk image's background for package_app.py, which runs it on
// every build with its own layout, so the picture always fits the window:
//
//   swift dmg_background.swift DIR WIDTH HEIGHT APP_X APP_Y APPLICATIONS_X APPLICATIONS_Y
//
// It writes DIR/background.png and DIR/background@2x.png, a WIDTH by HEIGHT
// point field in Apple's light gray with the SF Symbol arrow.right, light
// weight, in system gray, centred between the two icon centres (counted from
// the top left, as Finder counts them).
import AppKit

let arguments = Array(CommandLine.arguments.dropFirst())
let numbers = arguments.dropFirst().compactMap { Double($0) }
guard arguments.count == 7, numbers.count == 6 else {
    FileHandle.standardError.write(Data("usage: dmg_background.swift DIR WIDTH HEIGHT APP_X APP_Y APPLICATIONS_X APPLICATIONS_Y\n".utf8))
    exit(2)
}
let directory = URL(fileURLWithPath: arguments.first!, isDirectory: true)
let size = CGSize(width: numbers[0], height: numbers[1])
let appCentre = CGPoint(x: numbers[2], y: numbers[3])
let applicationsCentre = CGPoint(x: numbers[4], y: numbers[5])

func srgb(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat) -> NSColor {
    NSColor(srgbRed: red / 255, green: green / 255, blue: blue / 255, alpha: 1)
}

func draw() {
    // Apple's light gray (#F5F5F7).
    srgb(245, 245, 247).setFill()
    NSRect(origin: .zero, size: size).fill()

    // System gray (#8E8E93), the colour of secondary glyphs in light mode.
    let configuration = NSImage.SymbolConfiguration(pointSize: 64, weight: .light)
        .applying(.init(hierarchicalColor: srgb(142, 142, 147)))
    let arrow = NSImage(systemSymbolName: "arrow.right", accessibilityDescription: nil)!
        .withSymbolConfiguration(configuration)!
    // AppKit counts from the bottom left.
    let centre = CGPoint(
        x: (appCentre.x + applicationsCentre.x) / 2,
        y: size.height - (appCentre.y + applicationsCentre.y) / 2
    )
    arrow.draw(in: NSRect(
        x: centre.x - arrow.size.width / 2, y: centre.y - arrow.size.height / 2,
        width: arrow.size.width, height: arrow.size.height
    ))
}

func write(scale: CGFloat, to url: URL) throws {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale), pixelsHigh: Int(size.height * scale),
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .calibratedRGB, bytesPerRow: 0, bitsPerPixel: 0
    )!.retagging(with: .sRGB)!  // so Apple's sRGB values land unchanged
    // Finder places the picture by its size in points: 72 dpi at 1x, 144 at
    // 2x, the one a Retina display shows.
    rep.size = size
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    draw()
    NSGraphicsContext.restoreGraphicsState()
    try rep.representation(using: .png, properties: [:])!.write(to: url)
}

try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
try write(scale: 1, to: directory.appendingPathComponent("background.png"))
try write(scale: 2, to: directory.appendingPathComponent("background@2x.png"))
