import Combine
import Foundation

@MainActor
final class DashboardStore: ObservableObject {
    let appStore: AppStore

    private var sessions: [DeviceProfile.ID: DeviceDashboardSession] = [:]
    private var cancellables: Set<AnyCancellable> = []

    init(appStore: AppStore) {
        self.appStore = appStore
        appStore.deviceRegistry.$profiles
            .sink { [weak self] profiles in
                Task { @MainActor in
                    self?.pruneSessions(profiles: profiles)
                }
            }
            .store(in: &cancellables)
        appStore.operationCoordinator.$activeOperation
            .sink { [weak self] _ in
                Task { @MainActor in
                    guard let self else { return }
                    self.pruneSessions(profiles: self.appStore.deviceRegistry.profiles)
                }
            }
            .store(in: &cancellables)
    }

    func session(for profile: DeviceProfile) -> DeviceDashboardSession {
        if let session = sessions[profile.id] {
            return session
        }
        let session = DeviceDashboardSession(profile: profile, appStore: appStore)
        sessions[profile.id] = session
        objectWillChange.send()
        return session
    }

    func hasSession(for profileID: DeviceProfile.ID) -> Bool {
        sessions[profileID] != nil
    }

    private func pruneSessions(profiles: [DeviceProfile]) {
        let existingIDs = Set(profiles.map(\.id))
        let activeProfileID = appStore.operationCoordinator.activeOperation?.profileID
        sessions = sessions.filter { id, _ in
            existingIDs.contains(id) || id == activeProfileID
        }
    }
}
