import Foundation
import Network

enum LocalNetworkPreflightStatus: String, Equatable, Sendable {
    case allowed
    case denied
    case unknown
}

struct LocalNetworkPreflightResult: Equatable, Sendable {
    let status: LocalNetworkPreflightStatus
    let detail: String?
    let durationMilliseconds: Int
    let serviceType: String

    var telemetryFields: [String: JSONValue] {
        var fields: [String: JSONValue] = [
            "macos_local_network_preflight_result": .string(status.rawValue),
            "macos_local_network_preflight_duration_ms": .number(Double(durationMilliseconds)),
            "macos_local_network_preflight_service": .string(serviceType)
        ]
        if let detail, !detail.isEmpty {
            fields["macos_local_network_preflight_error"] = .string(detail)
        }
        return fields
    }
}

protocol LocalNetworkPreflightChecking: AnyObject {
    func check() async -> LocalNetworkPreflightResult
}

final class LocalNetworkPreflightResumeState: @unchecked Sendable {
    private let lock = NSLock()
    private var didResume = false
    private var didCancel = false
    private var cancellation: (@Sendable () -> Void)?

    func installCancellation(_ action: @escaping @Sendable () -> Void) {
        lock.lock()
        guard !didResume else {
            lock.unlock()
            return
        }
        let cancelled = didCancel
        if !cancelled { cancellation = action }
        lock.unlock()
        if cancelled { action() }
    }

    func cancel() {
        lock.lock()
        guard !didResume else {
            lock.unlock()
            return
        }
        didCancel = true
        let action = cancellation
        cancellation = nil
        lock.unlock()
        action?()
    }

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !didResume else {
            return false
        }
        didResume = true
        // The cancellation action captures finish, which owns this state and the browser.
        cancellation = nil
        return true
    }
}

final class BonjourLocalNetworkPreflightChecker: LocalNetworkPreflightChecking, @unchecked Sendable {
    private let serviceType: String
    private let timeoutNanoseconds: UInt64
    private let makeResumeState: @Sendable () -> LocalNetworkPreflightResumeState

    init(serviceType: String = "_airport._tcp", timeoutNanoseconds: UInt64 = 1_500_000_000,
         makeResumeState: @escaping @Sendable () -> LocalNetworkPreflightResumeState = { LocalNetworkPreflightResumeState() }) {
        self.serviceType = serviceType
        self.timeoutNanoseconds = timeoutNanoseconds
        self.makeResumeState = makeResumeState
    }

    func check() async -> LocalNetworkPreflightResult {
        let startedAt = Date()
        let serviceType = serviceType
        let timeoutNanoseconds = timeoutNanoseconds
        let resumeState = makeResumeState()
        return await withTaskCancellationHandler {
        await withCheckedContinuation { continuation in
            let queue = DispatchQueue(label: "TimeCapsuleSMB.LocalNetworkPreflight")
            let browser = NWBrowser(for: .bonjour(type: serviceType, domain: nil), using: .tcp)

            let finish: @Sendable (LocalNetworkPreflightStatus, String?) -> Void = { status, detail in
                queue.async {
                    guard resumeState.claim() else {
                        return
                    }
                    browser.stateUpdateHandler = nil
                    browser.browseResultsChangedHandler = nil
                    browser.cancel()
                    continuation.resume(returning: LocalNetworkPreflightResult(
                        status: status,
                        detail: detail,
                        durationMilliseconds: Self.elapsedMilliseconds(since: startedAt),
                        serviceType: serviceType
                    ))
                }
            }

            browser.stateUpdateHandler = { state in
                if let status = Self.outcome(for: state) {
                    finish(status, String(describing: state))
                }
            }
            browser.browseResultsChangedHandler = { results, _ in
                if !results.isEmpty {
                    finish(.allowed, nil)
                }
            }
            if Task.isCancelled {
                finish(.unknown, "cancelled")
                return
            }
            browser.start(queue: queue)
            // Cancellation can finish immediately; install it only after all browser
            // handlers are set so setup cannot restore a handler after cleanup.
            resumeState.installCancellation { finish(.unknown, "cancelled") }
            queue.asyncAfter(deadline: .now() + .nanoseconds(Int(timeoutNanoseconds))) {
                finish(.unknown, "timeout")
            }
        }
        } onCancel: {
            resumeState.cancel()
        }
    }

    private static func elapsedMilliseconds(since startedAt: Date) -> Int {
        max(0, Int(Date().timeIntervalSince(startedAt) * 1000))
    }

    static func outcome(for state: NWBrowser.State) -> LocalNetworkPreflightStatus? {
        switch state {
        case .waiting(let error):
            return isLocalNetworkPolicyDenied(error) ? .denied : nil
        case .failed(let error):
            return isLocalNetworkPolicyDenied(error) ? .denied : .unknown
        // Ready can precede the user's decision. Only actual results prove access.
        default:
            return nil
        }
    }

    static func isLocalNetworkPolicyDenied(_ error: NWError) -> Bool {
        if case .dns(let code) = error { return code == -65570 }
        return false
    }

}

enum LocalNetworkRecovery {
    static let settingsURL = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork")
}
