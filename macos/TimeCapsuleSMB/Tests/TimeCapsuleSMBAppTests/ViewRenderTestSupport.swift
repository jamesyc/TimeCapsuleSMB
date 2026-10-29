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
    let runner: any HelperRunning
    let composition: AppViewComposition

    init(responses: [StoreTestRunner.Response] = [], discoveryWaitsForReadiness: Bool = true) async throws {
        try await self.init(runner: StoreTestRunner(responses: responses), discoveryWaitsForReadiness: discoveryWaitsForReadiness)
    }

    init(runner: any HelperRunning, discoveryWaitsForReadiness: Bool = true) async throws {
        temp = try TemporaryDirectory()
        registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        self.runner = runner
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        passwordStore = InMemoryPasswordStore()
        let readinessStore = AppReadinessStore(backend: coordinator.backend)
        let deviceDiscovery = DeviceDiscoveryStore(
            coordinator: coordinator,
            readinessStore: discoveryWaitsForReadiness ? readinessStore : nil,
            registry: registry
        )
        appStore = AppStore(
            appReadinessStore: readinessStore,
            appSettingsStore: AppSettingsStore(settingsURL: temp.url.appendingPathComponent("app-settings.json")),
            deviceRegistry: registry,
            operationCoordinator: coordinator,
            passwordStore: passwordStore,
            deviceDiscovery: deviceDiscovery
        )
        composition = AppViewComposition(appStore: appStore)
    }

    var contentView: ContentView {
        ContentView(composition: composition, startsAutomatically: false)
    }

    func saveProfile(
        id: DeviceProfile.ID,
        host: String = "root@10.0.0.2",
        payloadFamily: String = "netbsd6_samba4",
        passwordState: DevicePasswordState = .available,
        password: String? = "pw"
    ) async throws -> DeviceProfile {
        let profile = try await registry.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: host, payloadFamily: payloadFamily),
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
    minimumDistinctPixelCount: Int = 8,
    file: StaticString = #filePath,
    line: UInt = #line
) throws -> NSBitmapImageRep {
    let bitmap = try renderView(view, size: size)
    XCTAssertGreaterThan(bitmap.pixelsWide, 0, file: file, line: line)
    XCTAssertGreaterThan(bitmap.pixelsHigh, 0, file: file, line: line)
    XCTAssertGreaterThan(
        sampledDistinctPixelCount(in: bitmap),
        minimumDistinctPixelCount,
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

    host.layoutSubtreeIfNeeded()
    host.displayIfNeeded()

    guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
        throw ViewRenderError.bitmapCreationFailed
    }
    host.cacheDisplay(in: host.bounds, to: bitmap)
    return bitmap
}

/// Keeps a view in an offscreen window, where SwiftUI keeps updating it and runs
/// its `onChange` handlers as the stores change; `renderView` draws it only once.
@MainActor
final class LiveHostedView {
    private let window: NSWindow

    init<V: View>(_ view: V, size: CGSize = CGSize(width: 1200, height: 800)) {
        window = NSWindow(
            contentRect: CGRect(origin: .zero, size: size),
            styleMask: [.titled],
            backing: .buffered,
            defer: false
        )
        window.isReleasedWhenClosed = false
        window.contentView = NSHostingView(rootView: view)
    }

    func close() {
        window.close()
    }
}

/// A text or control as VoiceOver reads it, and whether it is enabled.
struct AccessibleElement: Equatable {
    let role: NSAccessibility.Role
    let text: String
    let isEnabled: Bool
}

/// The texts and controls a view draws, in order, so a test can check the
/// words and enabled controls on screen instead of comparing pixels.
@MainActor
func accessibleElements<V: View>(_ view: V, size: CGSize = CGSize(width: 1200, height: 800)) -> [AccessibleElement] {
    // SwiftUI builds its accessibility tree only after assistive software asks
    // the app for one, as VoiceOver does by setting AXEnhancedUserInterface.
    // Its getter and setter are deprecated with no replacement, hence perform.
    // The setting is the whole app's, so it is left as it was found.
    let app = NSApplication.shared
    let attribute = "AXEnhancedUserInterface"
    let setValue = NSSelectorFromString("accessibilitySetValue:forAttribute:")
    let current = app.perform(NSSelectorFromString("accessibilityAttributeValue:"), with: attribute)?.takeUnretainedValue()
    let wasOn = (current as? NSNumber)?.boolValue ?? false
    if !wasOn {
        app.perform(setValue, with: NSNumber(value: true), with: attribute)
    }
    defer {
        if !wasOn {
            app.perform(setValue, with: NSNumber(value: false), with: attribute)
        }
    }
    let host = NSHostingView(rootView: view)
    host.frame = CGRect(origin: .zero, size: size)
    host.layoutSubtreeIfNeeded()

    var elements: [AccessibleElement] = []
    func collect(_ node: AnyObject) {
        if let role = node.accessibilityRole?() ?? nil, let text = accessibleText(of: node) {
            elements.append(AccessibleElement(role: role, text: text, isEnabled: node.isAccessibilityEnabled?() ?? true))
        }
        for child in (node.accessibilityChildren?() ?? nil) ?? [] {
            collect(child as AnyObject)
        }
    }
    collect(host)
    return elements
}

/// The texts on a device's page, read as `accessibleElements` reads them. Fails
/// when the read shows no device page, so a check that something is missing
/// from the page cannot pass on a read that found nothing.
@MainActor
func devicePageTexts<V: View>(_ view: V, file: StaticString = #filePath, line: UInt = #line) -> [String] {
    let texts = accessibleElements(view).map(\.text)
    let missingTabs = DeviceDashboardTab.allCases.map(\.title).filter { !texts.contains($0) }
    XCTAssertEqual(missingTabs, [], "The read shows no device page", file: file, line: line)
    return texts
}

/// A control's label, or a text's string, which SwiftUI gives as its value.
private func accessibleText(of node: AnyObject) -> String? {
    if let label = node.accessibilityLabel?() ?? nil, !label.isEmpty {
        return label
    }
    let value = (node.accessibilityValue as (() -> Any?)?)?() ?? nil
    return (value as? String).flatMap { $0.isEmpty ? nil : $0 }
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
