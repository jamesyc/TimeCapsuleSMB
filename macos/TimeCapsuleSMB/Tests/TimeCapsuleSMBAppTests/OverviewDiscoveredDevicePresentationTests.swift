import XCTest
@testable import TimeCapsuleSMBApp

final class OverviewDiscoveredDevicePresentationTests: XCTestCase {
    func testUnsavedSupportedDeviceOffersAdd() throws {
        let device = try discovered(supportedModel: true)

        let presentation = OverviewDiscoveredDevicePresentation(device: device, isSaved: false)

        XCTAssertEqual(presentation.statusText, L10n.string("overview.discovery.unsaved"))
        XCTAssertEqual(presentation.actionTitle, L10n.string("overview.discovery.add"))
        XCTAssertFalse(presentation.isUnsupported)
    }

    func testUnknownModelStillOffersAdd() throws {
        // No verdict from the helper (older helper or no usable syAP): configure decides.
        let device = try discovered(supportedModel: nil)

        let presentation = OverviewDiscoveredDevicePresentation(device: device, isSaved: false)

        XCTAssertEqual(presentation.actionTitle, L10n.string("overview.discovery.add"))
        XCTAssertFalse(presentation.isUnsupported)
    }

    func testSavedSupportedDeviceShowsSavedWithoutAction() throws {
        let presentation = OverviewDiscoveredDevicePresentation(device: try discovered(supportedModel: true), isSaved: true)

        XCTAssertEqual(presentation.statusText, L10n.string("overview.discovery.saved"))
        XCTAssertNil(presentation.actionTitle)
        XCTAssertFalse(presentation.isUnsupported)
    }

    func testUnsupportedModelIsNeverOfferedForAddingSavedOrNot() throws {
        let device = try discovered(supportedModel: false)

        for isSaved in [false, true] {
            let presentation = OverviewDiscoveredDevicePresentation(device: device, isSaved: isSaved)
            XCTAssertEqual(presentation.statusText, L10n.string("add_device.state.unsupported"))
            XCTAssertNotEqual(presentation.statusText, "add_device.state.unsupported")
            XCTAssertNil(presentation.actionTitle)
            XCTAssertTrue(presentation.isUnsupported)
        }
    }

    private func discovered(supportedModel: Bool?) throws -> DiscoveredDevice {
        let payload = try testDiscoveredDevice(syap: "115", supportedModel: supportedModel)
            .decode(DiscoveredDevicePayload.self)
        return DiscoveredDevice(payload: payload, index: 0)
    }
}
