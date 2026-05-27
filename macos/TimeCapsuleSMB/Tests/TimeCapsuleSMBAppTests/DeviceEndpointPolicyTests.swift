import XCTest
@testable import TimeCapsuleSMBApp

final class DeviceEndpointPolicyTests: XCTestCase {
    func testAddressFamilyParsesIPLiteralForms() {
        XCTAssertEqual(DeviceEndpointPolicy.addressFamily(for: "10.0.0.2"), .ipv4)
        XCTAssertEqual(DeviceEndpointPolicy.addressFamily(for: "fd00::2"), .ipv6)
        XCTAssertEqual(DeviceEndpointPolicy.addressFamily(for: "[fd00::2]"), .ipv6)
        XCTAssertEqual(DeviceEndpointPolicy.addressFamily(for: "fe80::1%en0"), .ipv6)
        XCTAssertNil(DeviceEndpointPolicy.addressFamily(for: "capsule.local"))
    }

    func testHostComponentNormalizesUserURLAndIPv6Wrappers() {
        XCTAssertEqual(DeviceEndpointPolicy.hostComponent("root@10.0.0.2"), "10.0.0.2")
        XCTAssertEqual(DeviceEndpointPolicy.hostComponent("root@[fd00::2]"), "fd00::2")
        XCTAssertEqual(DeviceEndpointPolicy.hostComponent("smb://admin@capsule.local/share"), "capsule.local")
        XCTAssertEqual(DeviceEndpointPolicy.hostComponent(" capsule.local. "), "capsule.local")
    }

    func testNormalizedHostKeyTreatsEquivalentTargetsAsEqual() {
        XCTAssertEqual(
            DeviceEndpointPolicy.normalizedHostKey("root@10.0.0.2"),
            DeviceEndpointPolicy.normalizedHostKey("10.0.0.2")
        )
        XCTAssertEqual(
            DeviceEndpointPolicy.normalizedHostKey("CAPSULE.local."),
            DeviceEndpointPolicy.normalizedHostKey("capsule.local")
        )
    }
}
