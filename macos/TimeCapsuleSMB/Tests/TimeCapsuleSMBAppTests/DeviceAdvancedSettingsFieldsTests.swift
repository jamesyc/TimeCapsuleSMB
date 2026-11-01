import AppKit
import SwiftUI
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeviceAdvancedSettingsFieldsTests: XCTestCase {
    private let signing = L10n.string("toggle.require_smb_signing")
    private let encryption = L10n.string("toggle.require_smb_encryption")
    private let turnOff = L10n.string("toggle.disable_smb_encryption")

    func testSecurityGroupShowsTheThreeCheckboxesInOrderWithTheirNotes() {
        let elements = accessibleElements(Harness(model: Model()))
        let texts = elements.map(\.text)

        let order = [signing, encryption, turnOff].compactMap { texts.firstIndex(of: $0) }
        XCTAssertEqual(order.count, 3, "every security checkbox is shown")
        XCTAssertEqual(order, order.sorted())
        for note in ["settings.note.signing", "settings.note.encryption", "settings.note.disable_encryption"] {
            XCTAssertTrue(texts.contains(L10n.string(note)), note)
        }
        XCTAssertFalse(texts.contains { $0.localizedCaseInsensitiveContains("signing and encryption") })
    }

    func testRequiringEncryptionDisablesTurnOffAndNeverSigning() {
        let cases: [(String, Model, [String: Bool])] = [
            ("defaults", Model(), [signing: true, encryption: true, turnOff: true]),
            ("encryption required", Model(requireSMBEncryption: true), [signing: true, encryption: true, turnOff: false]),
            ("encryption off", Model(disableSMBEncryption: true), [signing: true, encryption: false, turnOff: true]),
            ("any protocol", Model(anyProtocol: true), [signing: true, encryption: false, turnOff: true]),
            ("signing required", Model(requireSMBSigning: true), [signing: true, encryption: true, turnOff: true]),
        ]
        for (name, model, expected) in cases {
            let enabled = Dictionary(
                accessibleElements(Harness(model: model)).filter { expected[$0.text] != nil }.map { ($0.text, $0.isEnabled) },
                uniquingKeysWith: { first, _ in first }
            )
            XCTAssertEqual(enabled, expected, name)
        }
    }

    func testCheckingRequireSigningChangesOnlySigning() {
        let model = Model(disableSMBEncryption: true)
        let host = NSHostingView(rootView: Harness(model: model))
        host.frame = CGRect(x: 0, y: 0, width: 1200, height: 800)
        host.layoutSubtreeIfNeeded()

        XCTAssertTrue(pressAccessibleControl(in: host, labeled: signing), "Require SMB signing can be pressed")

        XCTAssertTrue(model.requireSMBSigning)
        XCTAssertTrue(model.disableSMBEncryption)
        XCTAssertFalse(model.requireSMBEncryption)
        XCTAssertFalse(model.anyProtocol)
    }
}

@MainActor
private final class Model: ObservableObject {
    @Published var rsyncEnabled = false
    @Published var internalShareUseDiskRoot = false
    @Published var smbBrowseCompatibility = false
    @Published var mdnsAdvertiseAFP = false
    @Published var anyProtocol: Bool
    @Published var requireSMBSigning: Bool
    @Published var requireSMBEncryption: Bool
    @Published var disableSMBEncryption: Bool
    @Published var fruitMetadataNetatalk = true
    @Published var vfsAIOForkEnabled = true
    @Published var debugLogging = false
    @Published var mountWaitSeconds = "30"
    @Published var ataIdleSeconds = "300"
    @Published var ataStandby = ""

    init(
        anyProtocol: Bool = false,
        requireSMBSigning: Bool = false,
        requireSMBEncryption: Bool = false,
        disableSMBEncryption: Bool = false
    ) {
        self.anyProtocol = anyProtocol
        self.requireSMBSigning = requireSMBSigning
        self.requireSMBEncryption = requireSMBEncryption
        self.disableSMBEncryption = disableSMBEncryption
    }
}

private struct Harness: View {
    @ObservedObject var model: Model

    var body: some View {
        DeviceAdvancedSettingsFields(
            rsyncEnabled: $model.rsyncEnabled,
            internalShareUseDiskRoot: $model.internalShareUseDiskRoot,
            smbBrowseCompatibility: $model.smbBrowseCompatibility,
            mdnsAdvertiseAFP: $model.mdnsAdvertiseAFP,
            anyProtocol: $model.anyProtocol,
            requireSMBSigning: $model.requireSMBSigning,
            requireSMBEncryption: $model.requireSMBEncryption,
            disableSMBEncryption: $model.disableSMBEncryption,
            fruitMetadataNetatalk: $model.fruitMetadataNetatalk,
            vfsAIOForkEnabled: $model.vfsAIOForkEnabled,
            debugLogging: $model.debugLogging,
            mountWaitSeconds: $model.mountWaitSeconds,
            ataIdleSeconds: $model.ataIdleSeconds,
            ataStandby: $model.ataStandby
        )
    }
}
