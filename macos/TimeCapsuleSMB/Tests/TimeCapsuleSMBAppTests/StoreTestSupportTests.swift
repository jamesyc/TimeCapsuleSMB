import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class StoreTestSupportTests: XCTestCase {
    func testGateHandlesCancellationBeforeRegistration() async throws {
        let gate = PauseGate()
        defer { gate.resumeAll() }
        let finished = expectation(description: "Cancelled task passes the gate")
        // This MainActor task cannot enter wait until this method suspends.
        let task = Task { @MainActor in
            await gate.wait()
            finished.fulfill()
        }
        task.cancel()
        await fulfillment(of: [finished], timeout: 2)
        gate.resumeAll()
        await task.value
        XCTAssertEqual(gate.waitingCount, 0)
    }

    func testGateHandlesCancellationAfterRegistration() async throws {
        let gate = PauseGate()
        defer { gate.resumeAll() }
        let finished = expectation(description: "Registered waiter is cancelled")
        let task = Task {
            await gate.wait()
            finished.fulfill()
        }
        try await waitUntilStoreState { gate.waitingCount == 1 }
        task.cancel()
        await fulfillment(of: [finished], timeout: 2)
        gate.resumeAll()
        await task.value
        XCTAssertEqual(gate.waitingCount, 0)
    }

    func testGateCancellationAndReleaseResumeEachWaiterOnce() async throws {
        let gate = PauseGate()
        defer { gate.resumeAll() }
        let finished = expectation(description: "Both waiters finish once")
        finished.expectedFulfillmentCount = 2
        finished.assertForOverFulfill = true
        let first = Task { await gate.wait(); finished.fulfill() }
        let second = Task { await gate.wait(); finished.fulfill() }
        try await waitUntilStoreState { gate.waitingCount == 2 }
        let release = Task.detached { gate.resumeAll() }
        first.cancel()
        await release.value
        await fulfillment(of: [finished], timeout: 2)
        await first.value
        await second.value
        XCTAssertEqual(gate.waitingCount, 0)
    }
}
