import AppKit
import SwiftUI
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
struct AppViewFixture {
    let temp: TemporaryDirectory
    let appStore: AppStore
    let registry: DeviceRegistryStore
    let passwordStore: InMemoryPasswordStore
    let runner: StoreTestRunner
    let composition: AppViewComposition

    init(responses: [StoreTestRunner.Response] = []) async throws {
        temp = try TemporaryDirectory()
        registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        runner = StoreTestRunner(responses: responses)
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        passwordStore = InMemoryPasswordStore()
        appStore = AppStore(
            appReadinessStore: AppReadinessStore(backend: coordinator.backend),
            appSettingsStore: AppSettingsStore(settingsURL: temp.url.appendingPathComponent("app-settings.json")),
            deviceRegistry: registry,
            operationCoordinator: coordinator,
            passwordStore: passwordStore
        )
        composition = AppViewComposition(appStore: appStore)
    }

    var contentView: ContentView {
        ContentView(composition: composition, startsAutomatically: false)
    }

    func saveProfile(
        id: DeviceProfile.ID,
        host: String = "root@10.0.0.2",
        passwordState: DevicePasswordState = .available,
        password: String? = "pw"
    ) async throws -> DeviceProfile {
        let profile = try await registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: host),
            discoveredDevice: nil,
            passwordState: passwordState,
            preferredID: id
        )
        if let password {
            try passwordStore.save(password, for: profile.keychainAccount)
        }
        return profile
    }

    func dashboardSession(for profile: DeviceProfile) -> DeviceDashboardSession {
        composition.dashboardStore.session(for: profile)
    }
}

@MainActor
@discardableResult
func assertRendersNonBlank<V: View>(
    _ view: V,
    size: CGSize = CGSize(width: 1200, height: 800),
    file: StaticString = #filePath,
    line: UInt = #line
) throws -> NSBitmapImageRep {
    let bitmap = try renderView(view, size: size)
    XCTAssertGreaterThan(bitmap.pixelsWide, 0, file: file, line: line)
    XCTAssertGreaterThan(bitmap.pixelsHigh, 0, file: file, line: line)
    XCTAssertGreaterThan(
        sampledDistinctPixelCount(in: bitmap),
        8,
        "Rendered view appears blank or visually uniform.",
        file: file,
        line: line
    )
    return bitmap
}

@MainActor
func renderView<V: View>(_ view: V, size: CGSize) throws -> NSBitmapImageRep {
    let host = NSHostingView(rootView: view)
    host.frame = CGRect(origin: .zero, size: size)
    host.wantsLayer = true
    host.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor

    let window = NSWindow(
        contentRect: host.frame,
        styleMask: [.borderless],
        backing: .buffered,
        defer: false
    )
    window.contentView = host
    window.layoutIfNeeded()
    host.layoutSubtreeIfNeeded()
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))

    guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
        throw ViewRenderError.bitmapCreationFailed
    }
    host.cacheDisplay(in: host.bounds, to: bitmap)
    return bitmap
}

private enum ViewRenderError: Error {
    case bitmapCreationFailed
}

private func sampledDistinctPixelCount(in bitmap: NSBitmapImageRep) -> Int {
    let width = bitmap.pixelsWide
    let height = bitmap.pixelsHigh
    guard width > 0, height > 0, let baseline = bitmap.colorAt(x: width / 2, y: height / 2) else {
        return 0
    }

    let xStep = max(width / 32, 1)
    let yStep = max(height / 32, 1)
    var distinct = 0
    for y in stride(from: 0, to: height, by: yStep) {
        for x in stride(from: 0, to: width, by: xStep) {
            guard let color = bitmap.colorAt(x: x, y: y) else {
                continue
            }
            if color.distance(from: baseline) > 0.08 {
                distinct += 1
            }
        }
    }
    return distinct
}

private extension NSColor {
    func distance(from other: NSColor) -> CGFloat {
        let left = usingColorSpace(.deviceRGB) ?? self
        let right = other.usingColorSpace(.deviceRGB) ?? other
        return abs(left.redComponent - right.redComponent)
            + abs(left.greenComponent - right.greenComponent)
            + abs(left.blueComponent - right.blueComponent)
            + abs(left.alphaComponent - right.alphaComponent)
    }
}
