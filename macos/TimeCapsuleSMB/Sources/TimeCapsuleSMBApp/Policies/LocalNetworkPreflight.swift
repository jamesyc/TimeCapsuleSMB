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

    var allowsConfigure: Bool {
        status != .denied
    }

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

private final class LocalNetworkPreflightResumeState: @unchecked Sendable {
    private let lock = NSLock()
    private var didResume = false

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !didResume else {
            return false
        }
        didResume = true
        return true
    }
}

final class BonjourLocalNetworkPreflightChecker: LocalNetworkPreflightChecking, @unchecked Sendable {
    private let serviceType: String
    private let timeoutNanoseconds: UInt64

    init(serviceType: String = "_airport._tcp", timeoutNanoseconds: UInt64 = 1_500_000_000) {
        self.serviceType = serviceType
        self.timeoutNanoseconds = timeoutNanoseconds
    }

    func check() async -> LocalNetworkPreflightResult {
        let startedAt = Date()
        let serviceType = serviceType
        let timeoutNanoseconds = timeoutNanoseconds
        return await withCheckedContinuation { continuation in
            let queue = DispatchQueue(label: "TimeCapsuleSMB.LocalNetworkPreflight")
            let browser = NWBrowser(for: .bonjour(type: serviceType, domain: nil), using: .tcp)
            let resumeState = LocalNetworkPreflightResumeState()

            let finish: @Sendable (LocalNetworkPreflightStatus, String?) -> Void = { status, detail in
                queue.async {
                    guard resumeState.claim() else {
                        return
                    }
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
                switch state {
                case .ready:
                    finish(.allowed, nil)
                case .waiting(let error):
                    if Self.isLocalNetworkPolicyDenied(error) {
                        finish(.denied, String(describing: error))
                    }
                case .failed(let error):
                    finish(
                        Self.isLocalNetworkPolicyDenied(error) ? .denied : .unknown,
                        String(describing: error)
                    )
                case .cancelled, .setup:
                    break
                @unknown default:
                    break
                }
            }
            browser.browseResultsChangedHandler = { results, _ in
                if !results.isEmpty {
                    finish(.allowed, nil)
                }
            }
            browser.start(queue: queue)
            queue.asyncAfter(deadline: .now() + .nanoseconds(Int(timeoutNanoseconds))) {
                finish(.unknown, "timeout")
            }
        }
    }

    private static func elapsedMilliseconds(since startedAt: Date) -> Int {
        max(0, Int(Date().timeIntervalSince(startedAt) * 1000))
    }

    private static func isLocalNetworkPolicyDenied(_ error: NWError) -> Bool {
        let text = String(describing: error).lowercased()
        return text.contains("policy")
            || text.contains("denied")
            || text.contains("privacy")
            || text.contains("permission")
            || text.contains("-65570")
    }
}

enum LocalNetworkRecovery {
    static let settingsURL = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork")
}
