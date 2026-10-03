import XCTest
import Network
@testable import TimeCapsuleSMBApp

final class LocalNetworkPreflightTests: XCTestCase {
    func testReadyIsInconclusiveAndOnlyStructuredPolicyDenialIsDenied() {
        XCTAssertNil(BonjourLocalNetworkPreflightChecker.outcome(for: .ready))
        XCTAssertEqual(BonjourLocalNetworkPreflightChecker.outcome(for: .waiting(.dns(-65570))), .denied)
        XCTAssertEqual(BonjourLocalNetworkPreflightChecker.outcome(for: .failed(.dns(-65570))), .denied)
        XCTAssertEqual(BonjourLocalNetworkPreflightChecker.outcome(for: .failed(.posix(.ENETDOWN))), .unknown)
        XCTAssertNil(BonjourLocalNetworkPreflightChecker.outcome(for: .waiting(.posix(.ENETDOWN))))
        XCTAssertFalse(BonjourLocalNetworkPreflightChecker.isLocalNetworkPolicyDenied(.dns(-65554)))
    }

    func testCompletionReleasesCancellationCaptures() {
        var state: LocalNetworkPreflightResumeState? = LocalNetworkPreflightResumeState()
        var token: PreflightLifetimeToken? = PreflightLifetimeToken()
        weak var weakState = state
        weak var weakToken = token
        let calls = PreflightCallCounter()
        installReentrantCancellation(on: state!, token: token!, calls: calls)
        token = nil
        XCTAssertNotNil(weakToken)
        XCTAssertTrue(state!.claim())
        state!.cancel()
        state!.cancel()
        XCTAssertFalse(state!.claim())
        XCTAssertEqual(calls.cancellations, 0)
        XCTAssertNil(weakToken)
        state = nil
        XCTAssertNil(weakState)
    }

    func testCancellationTakesCallbackOnceAndReleasesCaptures() {
        var state: LocalNetworkPreflightResumeState? = LocalNetworkPreflightResumeState()
        var token: PreflightLifetimeToken? = PreflightLifetimeToken()
        weak var weakState = state
        weak var weakToken = token
        let calls = PreflightCallCounter()
        installReentrantCancellation(on: state!, token: token!, calls: calls)
        token = nil
        state!.cancel()
        state!.cancel()
        XCTAssertEqual(calls.cancellations, 1)
        XCTAssertEqual(calls.completions, 1)
        XCTAssertFalse(state!.claim())
        XCTAssertNil(weakToken)
        state = nil
        XCTAssertNil(weakState)
    }

    func testCancellationBeforeRegistrationInvokesWithoutRetainingCallback() {
        var state: LocalNetworkPreflightResumeState? = LocalNetworkPreflightResumeState()
        state!.cancel()
        state!.cancel()
        var token: PreflightLifetimeToken? = PreflightLifetimeToken()
        weak var weakState = state
        weak var weakToken = token
        let calls = PreflightCallCounter()
        installReentrantCancellation(on: state!, token: token!, calls: calls)
        token = nil
        state!.cancel()
        XCTAssertEqual(calls.cancellations, 1)
        XCTAssertEqual(calls.completions, 1)
        XCTAssertNil(weakToken)
        state = nil
        XCTAssertNil(weakState)
    }

    func testRegistrationAfterCompletionDoesNotRetainOrInvokeCallback() {
        var state: LocalNetworkPreflightResumeState? = LocalNetworkPreflightResumeState()
        XCTAssertTrue(state!.claim())
        var token: PreflightLifetimeToken? = PreflightLifetimeToken()
        weak var weakState = state
        weak var weakToken = token
        let calls = PreflightCallCounter()
        installReentrantCancellation(on: state!, token: token!, calls: calls)
        token = nil
        state!.cancel()
        XCTAssertEqual(calls.cancellations, 0)
        XCTAssertNil(weakToken)
        state = nil
        XCTAssertNil(weakState)
    }

    @MainActor
    func testCompletionRacingCancellationResumesOnceAndReleasesState() async throws {
        for _ in 0..<20 {
            var state: LocalNetworkPreflightResumeState? = LocalNetworkPreflightResumeState()
            weak var weakState = state
            let calls = PreflightCallCounter()
            installReentrantCancellation(on: state!, token: PreflightLifetimeToken(), calls: calls)
            XCTAssertTrue(raceCompletionAndCancellation(state!, calls: calls))
            XCTAssertEqual(calls.completions, 1)
            state = nil
            try await waitUntilStoreState { weakState == nil }
            if weakState != nil { break }
        }
    }

    @MainActor
    func testAlreadyCancelledProbeCompletesAndReleasesItsActualCallbackState() async throws {
        let lifetime = PreflightStateLifetime()
        let checker = BonjourLocalNetworkPreflightChecker(timeoutNanoseconds: 30_000_000_000,
                                                          makeResumeState: { lifetime.makeState() })
        let task = Task { await checker.check() }
        task.cancel()
        let result = await task.value
        XCTAssertEqual(result.status, .unknown)
        XCTAssertEqual(result.detail, "cancelled")
        XCTAssertEqual(lifetime.createdCount, 1)
        // The queued duplicate finish must drain, but this path schedules no timeout or browse.
        try await waitUntilStoreState { lifetime.isReleased }
        XCTAssertTrue(lifetime.isReleased)
    }
}

private final class PreflightLifetimeToken: @unchecked Sendable {}

private final class PreflightCallCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelCount = 0
    private var completionCount = 0
    var cancellations: Int { lock.lock(); defer { lock.unlock() }; return cancelCount }
    var completions: Int { lock.lock(); defer { lock.unlock() }; return completionCount }
    func cancelled() { lock.lock(); cancelCount += 1; lock.unlock() }
    func completed() { lock.lock(); completionCount += 1; lock.unlock() }
}

private func installReentrantCancellation(on state: LocalNetworkPreflightResumeState,
                                          token: PreflightLifetimeToken, calls: PreflightCallCounter) {
    state.installCancellation {
        withExtendedLifetime(token) {
            calls.cancelled()
            if state.claim() { calls.completed() }
        }
    }
}

private func raceCompletionAndCancellation(_ state: LocalNetworkPreflightResumeState,
                                          calls: PreflightCallCounter) -> Bool {
    let started = DispatchSemaphore(value: 0)
    let group = DispatchGroup()
    group.enter()
    DispatchQueue.global().async {
        defer { group.leave() }
        started.wait()
        if state.claim() { calls.completed() }
    }
    group.enter()
    DispatchQueue.global().async {
        defer { group.leave() }
        started.wait()
        state.cancel()
    }
    started.signal()
    started.signal()
    return group.wait(timeout: .now() + 5) == .success
}

private final class PreflightStateLifetime: @unchecked Sendable {
    private let lock = NSLock()
    private weak var state: LocalNetworkPreflightResumeState?
    private var count = 0
    var createdCount: Int { lock.lock(); defer { lock.unlock() }; return count }
    var isReleased: Bool { lock.lock(); defer { lock.unlock() }; return count > 0 && state == nil }
    func makeState() -> LocalNetworkPreflightResumeState {
        let created = LocalNetworkPreflightResumeState()
        lock.lock()
        state = created
        count += 1
        lock.unlock()
        return created
    }
}
