import Combine
import Foundation

enum AppUpdateState: String, Equatable {
    case idle
    case checking
    case current
    case unavailable
    case updateAvailable
    case failed

    var title: String {
        switch self {
        case .idle:
            return L10n.string("app_update.state.idle")
        case .checking:
            return L10n.string("app_update.state.checking")
        case .current:
            return L10n.string("app_update.state.current")
        case .unavailable:
            return L10n.string("app_update.state.unavailable")
        case .updateAvailable:
            return L10n.string("app_update.state.update_available")
        case .failed:
            return L10n.string("app_update.state.failed")
        }
    }
}

@MainActor
final class AppUpdateStore: ObservableObject {
    @Published private(set) var state: AppUpdateState = .idle
    @Published private(set) var payload: AppUpdateResult?
    @Published private(set) var error: BackendErrorViewModel?
    @Published var notice: AppUpdateNotice?

    private let checker: AppReleaseChecking
    private let installedVersion: String?

    init(
        checker: AppReleaseChecking = GitHubReleaseChecker(),
        installedVersion: String? = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
    ) {
        self.checker = checker
        self.installedVersion = installedVersion
    }

    var isChecking: Bool {
        state == .checking
    }

    func checkNow(automatically: Bool = false) {
        guard !isChecking else { return }
        state = .checking
        payload = nil
        error = nil
        notice = nil

        Task {
            do {
                guard let installedVersion, let installed = AppReleaseVersion(installedVersion) else {
                    throw AppUpdateError.installedVersionUnavailable
                }
                let release = try await checker.latestRelease()
                guard let latest = AppReleaseVersion(release.tag), !latest.isPrerelease else {
                    throw AppUpdateError.invalidRelease
                }
                let result = AppUpdateResult(
                    installedVersion: installedVersion,
                    release: release,
                    updateAvailable: latest.isNewerStableRelease(than: installed)
                )
                payload = result
                state = result.updateAvailable ? .updateAvailable : .current
                if result.updateAvailable {
                    notice = .available(result)
                } else if !automatically {
                    notice = .current
                }
            } catch {
                self.error = BackendErrorViewModel(
                    operation: "release-check",
                    code: "release_check_failed",
                    message: error.localizedDescription
                )
                state = .failed
                if !automatically {
                    notice = .failed(error.localizedDescription)
                }
            }
        }
    }

    func downloadOpeningFailed() {
        let message = AppUpdateError.openDownloadFailed.localizedDescription
        error = BackendErrorViewModel(operation: "release-check", code: "open_download_failed", message: message)
        notice = .failed(message)
    }
}

struct AppUpdateResult: Equatable {
    let installedVersion: String
    let release: GitHubAppRelease
    let updateAvailable: Bool
}

enum AppUpdateNotice: Equatable {
    case available(AppUpdateResult)
    case current
    case failed(String)

    var title: String {
        switch self {
        case .available:
            return L10n.string("app_update.alert.available_title")
        case .current:
            return AppUpdateState.current.title
        case .failed:
            return AppUpdateState.failed.title
        }
    }

    var message: String {
        switch self {
        case .available(let result):
            return L10n.format("app_update.alert.available_message", result.release.tag, result.installedVersion)
        case .current:
            return L10n.string("app_update.alert.current_message")
        case .failed(let message):
            return message
        }
    }
}
