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

    func testBootHookStepDriverThrowsTheFailedStepsError() async throws {
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(failure: [testBootHookError("plan failed")]))
        try await fixture.run(.backup)

        await assertBootHookFixtureFails(.failed(.plan(.patch), "plan failed")) {
            try await fixture.run(.plan(.patch))
        }
        XCTAssertNotNil(fixture.store.backup)
        XCTAssertNil(fixture.store.plan)
    }

    func testBootHookStepDriverThrowsForAStepTheStoreRefuses() async throws {
        let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner.pausing(testBootHookResponses(), before: { _ in true }))
        defer { fixture.runner.finishAll() }
        fixture.maintenanceStore.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
        try await waitUntilStoreState { fixture.runner.calls.count == 1 }

        // A second backup while the first runs never starts.
        await assertBootHookFixtureFails(.refused(.backup, WorkflowLocalError.operationAlreadyRunning.message)) {
            try await fixture.run(.backup)
        }
        XCTAssertEqual(fixture.runner.calls.count, 1)
    }

    func testBootHookStepDriverThrowsWhenAStepEndsWithoutItsPayload() async throws {
        let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner(responses: testBootHookResponses() + [
            .init(
                events: [testBootHookResult(testBootHookPlanPayload(mode: .patch))],
                pauseBeforeEvents: true
            )
        ]))
        defer { fixture.runner.finishAll() }
        try await fixture.run(.backup)

        // A firmware choice changed while planning drops the finished plan.
        let planRun = Task { try await fixture.run(.plan(.patch)) }
        try await waitUntilStoreState { fixture.runner.calls.count == 2 }
        fixture.store.firmwareVersion = "7.8.2"
        fixture.runner.finishAll()

        await assertBootHookFixtureFails(.endedWithoutPayload(.plan(.patch))) {
            try await planRun.value
        }
        XCTAssertNil(fixture.store.error)
    }

    func testBootHookPayloadsShowTheHelpersKeyNotItsEnglishSummary() async throws {
        let store = try await BootHookTestFixture.afterPlan(
            testBootHookPlanPayload(mode: .patch)
        ).store

        XCTAssertEqual(store.plan?.summary, helperEnglishSummaryStandIn)
        XCTAssertEqual(store.plan?.localizedSummary, "Flash patch write plan generated.")
    }

    private func assertBootHookFixtureFails(
        _ expected: BootHookFixtureFailure,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ body: () async throws -> Void
    ) async {
        do {
            try await body()
            XCTFail("expected \(expected)", file: file, line: line)
        } catch let failure as BootHookFixtureFailure {
            XCTAssertEqual(failure, expected, file: file, line: line)
        } catch {
            XCTFail("unexpected \(error)", file: file, line: line)
        }
    }
}
