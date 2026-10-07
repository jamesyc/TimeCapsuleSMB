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

    func testScopedLinkLocalTargetsKeepTheirZone() {
        // The backend saves root@fe80::…%en0 when only link-local IPv6 answers.
        let target = "root@fe80::82ea:96ff:fee6:5868%en0"
        XCTAssertEqual(DeviceEndpointPolicy.hostComponent(target), "fe80::82ea:96ff:fee6:5868%en0")
        XCTAssertEqual(DeviceEndpointPolicy.rootSSHTarget(target), target)
        XCTAssertEqual(DeviceEndpointPolicy.rootSSHTarget("fe80::1%en0"), "root@fe80::1%en0")
        XCTAssertEqual(DeviceEndpointPolicy.normalizedHostKey(target), "ipv6:fe80::82ea:96ff:fee6:5868%en0")
        XCTAssertNotEqual(DeviceEndpointPolicy.normalizedHostKey("root@fe80::1%en0"), DeviceEndpointPolicy.normalizedHostKey("root@fe80::1%en1"))
        XCTAssertEqual(DeviceEndpointPolicy.smbURL(host: "fe80::1%en0", account: "admin")?.absoluteString, "smb://admin@[fe80::1%25en0]")
        // IPv4 is unchanged.
        XCTAssertEqual(DeviceEndpointPolicy.rootSSHTarget("10.0.0.2"), "root@10.0.0.2")
        XCTAssertEqual(DeviceEndpointPolicy.normalizedHostKey("root@10.0.0.2"), "ipv4:10.0.0.2")
        XCTAssertEqual(DeviceEndpointPolicy.smbURL(host: "10.0.0.2", account: "admin")?.absoluteString, "smb://admin@10.0.0.2")
    }

    func testAddressSummaryListsALinkLocalAddressOnlyWhenItIsTheTarget() {
        let addresses = ["192.168.1.218", "169.254.155.207", "fe80::82ea:96ff:fee6:5868%en0"]
            .compactMap { DeviceNetworkAddress(value: $0, source: .bonjour) }

        XCTAssertEqual(DeviceEndpointPolicy.addressSummary(addresses), "IPv4 192.168.1.218")
        XCTAssertEqual(DeviceEndpointPolicy.addressSummary(addresses, target: "root@192.168.1.218"), "IPv4 192.168.1.218")
        XCTAssertEqual(
            DeviceEndpointPolicy.addressSummary(addresses, target: "root@fe80::82ea:96ff:fee6:5868%en0"),
            "IPv4 192.168.1.218  IPv6 fe80::82ea:96ff:fee6:5868%en0 link-local"
        )
        // The same address on another interface is not the target.
        XCTAssertEqual(DeviceEndpointPolicy.addressSummary(addresses, target: "root@fe80::82ea:96ff:fee6:5868%en1"), "IPv4 192.168.1.218")
        // With no regular address, every link-local one shows, as before.
        let linkLocalOnly = ["169.254.155.207", "fe80::1%en0"].compactMap { DeviceNetworkAddress(value: $0, source: .bonjour) }
        XCTAssertEqual(DeviceEndpointPolicy.addressSummary(linkLocalOnly), "IPv4 169.254.155.207 link-local  IPv6 fe80::1%en0 link-local")
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
