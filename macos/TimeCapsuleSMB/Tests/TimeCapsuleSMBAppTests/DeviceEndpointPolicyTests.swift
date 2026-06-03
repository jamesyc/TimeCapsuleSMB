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

    func testHostComponentStripsPortsWithoutBreakingIPv6Literals() {
        XCTAssertEqual(DeviceEndpointPolicy.hostComponent("root@10.0.0.2:22"), "10.0.0.2")
        XCTAssertEqual(DeviceEndpointPolicy.hostComponent("capsule.local:445"), "capsule.local")
        XCTAssertEqual(DeviceEndpointPolicy.hostComponent("smb://admin@capsule.local:445/share"), "capsule.local")
        XCTAssertEqual(DeviceEndpointPolicy.hostComponent("root@[fd00::2]:22"), "fd00::2")
        XCTAssertEqual(DeviceEndpointPolicy.hostComponent("fd00::2"), "fd00::2")
    }

    func testRootSSHTargetCanonicalizesDefaultPortButPreservesUnsupportedPortsForBackendValidation() {
        XCTAssertEqual(DeviceEndpointPolicy.rootSSHTarget("10.0.0.2:22"), "root@10.0.0.2")
        XCTAssertEqual(DeviceEndpointPolicy.rootSSHTarget("admin@capsule.local:22"), "admin@capsule.local")
        XCTAssertEqual(DeviceEndpointPolicy.rootSSHTarget("root@[fd00::2]:22"), "root@fd00::2")
        XCTAssertEqual(DeviceEndpointPolicy.rootSSHTarget("10.0.0.2:2222"), "root@10.0.0.2:2222")
        XCTAssertEqual(DeviceEndpointPolicy.rootSSHTarget("[fd00::2]:2222"), "root@[fd00::2]:2222")
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
        XCTAssertEqual(
            DeviceEndpointPolicy.normalizedHostKey("root@capsule.local:445"),
            DeviceEndpointPolicy.normalizedHostKey("capsule.local")
        )
    }
}
