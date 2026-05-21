import SwiftUI

struct CheckupTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let showDiagnostics: () -> Void

    var body: some View {
        let store = session.doctorStore
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("dashboard.tab.checkup"))
                .font(.title2.weight(.semibold))
            HStack {
                TextField(L10n.string("field.bonjour_timeout"), text: $session.doctorStore.bonjourTimeout)
                    .frame(width: 180)
                Button {
                    session.runCheckup(profile: profile)
                } label: {
                    Label(L10n.string("dashboard.action.run_checkup"), systemImage: "stethoscope")
                }
                .disabled(store.isRunning || store.bonjourTimeoutValue == nil)
                Label(store.state.title, systemImage: "circle")
            }
            if let stage = store.currentStage {
                StageLine(stage: stage)
            }
            if let summary = store.summary {
                let presentation = CheckupPresentation(summary: summary, state: store.state)
                Text(presentation.headline)
                    .font(.headline)
                SummaryGrid(rows: presentation.summaryRows.map { ($0.label, $0.value) })
                ForEach(presentation.groups) { group in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(group.domain).font(.headline)
                        ForEach(Array(group.checks.enumerated()), id: \.offset) { _, check in
                            HStack {
                                Text(check.status)
                                    .font(.system(.caption, design: .monospaced))
                                    .frame(width: 44, alignment: .leading)
                                Text(check.message)
                                    .font(.caption)
                            }
                        }
                    }
                }
            }
            if let error = store.error {
                ErrorRecoveryView(error: error) { action in
                    handleRecovery(action: action, error: error)
                }
            }
        }
    }

    private func handleRecovery(action: RecoveryAction, error: BackendErrorViewModel) {
        if action.kind == .diagnostics {
            showDiagnostics()
            return
        }
        _ = session.handleRecoveryAction(action, error: error, profile: profile)
    }
}
