import XCTest
@testable import TimeCapsuleSMBApp

final class AddDevicePresentationTests: XCTestCase {
    func testProgressPresentationAppearsOnlyForBlockingStates() {
        let discoveryStage = OperationStageState(event: BackendEvent(
            type: "stage",
            operation: "discover",
            stage: "browse_bonjour",
            cancellable: true,
            description: "Browsing Bonjour services."
        ))
        let configureStage = OperationStageState(event: BackendEvent(
            type: "stage",
            operation: "configure",
            stage: "ssh_probe",
            cancellable: true,
            description: "Checking SSH access."
        ))

        let discovering = AddDeviceProgressPresentation(state: .discovering, currentStage: discoveryStage)
        XCTAssertEqual(discovering?.title, "Discovering Apple AirPort devices")
        XCTAssertEqual(discovering?.message, "Browsing for nearby AirPort Bonjour services...")
        XCTAssertNil(discovering?.detail)

        let configuring = AddDeviceProgressPresentation(state: .configuring, currentStage: configureStage)
        XCTAssertEqual(configuring?.title, "Connecting to Apple AirPort device")
        XCTAssertEqual(configuring?.detail, "Checking SSH")

        let saving = AddDeviceProgressPresentation(state: .savingProfile, currentStage: nil)
        XCTAssertEqual(saving?.title, "Saving Device")
        XCTAssertNil(saving?.detail)

        for state in AddDeviceFlowState.allCases where ![.discovering, .configuring, .savingProfile].contains(state) {
            XCTAssertNil(AddDeviceProgressPresentation(state: state, currentStage: discoveryStage), "\(state) should not show a blocking progress modal.")
        }
    }
}
