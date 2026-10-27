import AppKit
import SwiftUI

struct MaintenanceTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let showDiagnostics: () -> Void
    let diagnosticsText: () -> String

    var body: some View {
        let store = session.maintenanceStore
        let presentation = MaintenanceDashboardPresentation(
            store: store,
            flashStore: session.flashStore,
            profile: profile
        )

        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text(L10n.string("dashboard.tab.maintenance"))
                    .font(.title2.weight(.semibold))

                MaintenanceCardsView(cards: presentation.cards) { card in
                    session.maintenanceStore.select(card)
                }

                switch presentation.selectedCard {
                case .bootHook:
                    FlashBootHookSection(
                        store: session.flashStore,
                        performAction: { action in
                            session.performFlashAction(action, profile: profile)
                        },
                        chooseFirmwareTemplate: {
                            chooseFirmwareTemplate(store: session.flashStore)
                        },
                        recoveryAction: { kind, error in recoveryAction(kind, error: error) },
                        diagnosticsText: diagnosticsText
                    )
                case .workflow:
                    MaintenanceDetailView(
                        presentation: presentation.detail,
                        store: store,
                        performAction: { action in
                            session.performMaintenanceAction(action, profile: profile, showDiagnostics: showDiagnostics)
                        }
                    )

                    if let error = store.error(for: presentation.detail.workflow) {
                        ErrorRecoveryView(
                            error: error,
                            diagnosticsText: diagnosticsText
                        ) { recoveryAction($0, error: error) }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        // The flash store's eligibility comes from the profile and the build
        // policy; refresh it before the boot hook card and panel read it.
        .onAppear {
            refreshFlashEligibility()
        }
        .onChange(of: profile.id) { _, _ in
            refreshFlashEligibility()
        }
    }

    private func refreshFlashEligibility() {
        if FlashBootHookVisibilityPolicy.isVisible(for: profile) {
            session.flashStore.refresh(profile: profile)
        }
    }

    private func recoveryAction(_ kind: RecoveryActionKind, error: BackendErrorViewModel) -> (() -> Void)? {
        kind == .diagnostics ? { showDiagnostics() } : session.recoveryAction(kind, error: error, profile: profile)
    }

    private func chooseFirmwareTemplate(store: FlashWorkflowStore) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.prompt = L10n.string("maintenance.action.choose")
        panel.begin { response in
            guard response == .OK, let url = panel.url else {
                return
            }
            Task { @MainActor in
                store.firmwareTemplatePath = url.path
            }
        }
    }
}

private struct MaintenanceCardsView: View {
    let cards: [MaintenanceCardPresentation]
    let select: (MaintenanceCard) -> Void
    // The tallest card's content: every card takes it as its minimum, so the
    // cards stay even when one subtitle wraps further than the rest.
    @State private var contentHeight: CGFloat = 0

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 190), spacing: 10, alignment: .top)], alignment: .leading, spacing: 10) {
            ForEach(cards) { card in
                Button {
                    select(card.card)
                } label: {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text(card.title)
                                .font(.headline)
                            Spacer()
                            Image(systemName: card.isSelected ? "checkmark.circle.fill" : "circle")
                        }
                        // No line limit: longer translations need more lines
                        // in four narrow columns than a fixed limit allows.
                        Text(card.subtitle)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(card.stateTitle)
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.secondary)
                    }
                    .background {
                        GeometryReader { proxy in
                            Color.clear.preference(key: MaintenanceCardHeightKey.self, value: proxy.size.height)
                        }
                    }
                    .frame(maxWidth: .infinity, minHeight: max(92, contentHeight), alignment: .topLeading)
                    .padding(10)
                    .background(card.isSelected ? Color.accentColor.opacity(0.14) : Color.secondary.opacity(0.08))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
            }
        }
        .onPreferenceChange(MaintenanceCardHeightKey.self) { height in
            contentHeight = height
        }
    }
}

private struct MaintenanceCardHeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0

    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

private struct MaintenanceDetailView: View {
    let presentation: MaintenanceWorkflowDetailPresentation
    @ObservedObject var store: MaintenanceStore
    let performAction: (MaintenanceUserAction) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(presentation.title)
                        .font(.headline)
                    Text(presentation.subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Text(presentation.stateTitle)
                    .font(.caption.weight(.medium))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 5)
                    .background(.quaternary)
                    .clipShape(Capsule())
            }

            Label(presentation.risk, systemImage: "exclamationmark.triangle")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(presentation.statusMessage)
                .font(.callout)
                .foregroundStyle(.secondary)

            if presentation.workflow == .fsck {
                FsckTargetListView(store: store)
            }

            HStack {
                ForEach(presentation.actions) { action in
                    MaintenanceActionButton(
                        action: action,
                        isEnabled: presentation.isEnabled(action),
                        perform: performAction
                    )
                }
            }

            if let timeline = presentation.timeline, !timeline.items.isEmpty {
                MaintenanceTimelineView(presentation: timeline)
            }

            if let plan = presentation.plan {
                MaintenancePlanView(presentation: plan)
            }

            if let completion = presentation.completion {
                MaintenanceCompletionView(presentation: completion)
            }

            // Wipe Internal Disk mounts nothing and always restarts and waits.
            if presentation.workflow != .wipeDisk {
                MaintenanceAdvancedOptionsView(workflow: presentation.workflow, store: store)
            }
        }
        .padding(10)
        .background(Color.secondary.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

}

private struct MaintenanceActionButton: View {
    let action: MaintenanceUserAction
    let isEnabled: Bool
    let perform: (MaintenanceUserAction) -> Void

    var body: some View {
        if action.isCommitAction {
            Button {
                perform(action)
            } label: {
                Label(action.title, systemImage: action.systemImage)
            }
            .buttonStyle(.borderedProminent)
            .disabled(!isEnabled)
        } else {
            Button {
                perform(action)
            } label: {
                Label(action.title, systemImage: action.systemImage)
            }
            .buttonStyle(.bordered)
            .disabled(!isEnabled)
        }
    }
}

private struct FsckTargetListView: View {
    @ObservedObject var store: MaintenanceStore

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if store.fsckTargets.isEmpty {
                Text(L10n.string("maintenance.fsck.no_volumes"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(store.fsckTargets) { target in
                    Button {
                        store.selectedFsckTargetID = target.id
                    } label: {
                        HStack {
                            Image(systemName: store.selectedFsckTargetID == target.id ? "checkmark.circle.fill" : "circle")
                            Text(target.name ?? target.device)
                            Text(target.mountpoint).foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}

private struct MaintenancePlanView: View {
    let presentation: MaintenancePlanPresentation

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(presentation.title)
                .font(.headline)
            SummaryGrid(rows: presentation.rows.map { ($0.label, $0.value) })
            ForEach(presentation.warnings, id: \.self) { warning in
                Label(warning, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.yellow)
            }
        }
    }
}

private struct MaintenanceCompletionView: View {
    let presentation: MaintenanceCompletionPresentation

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(presentation.title)
                .font(.headline)
            SummaryGrid(rows: presentation.rows.map { ($0.label, $0.value) })
        }
    }
}

struct MaintenanceTimelineView: View {
    let presentation: MaintenanceTimelinePresentation

    var body: some View {
        OperationTimelineListView(
            title: L10n.string("maintenance.timeline.title"),
            items: presentation.items
        )
    }
}

private struct MaintenanceAdvancedOptionsView: View {
    let workflow: MaintenanceWorkflow
    @ObservedObject var store: MaintenanceStore

    var body: some View {
        DashboardDisclosureSection(title: L10n.string("maintenance.advanced_options")) {
            if workflow == .sshAccess {
                SSHAccessAdvancedOptionsView(store: store)
            } else {
                RemoteMaintenanceAdvancedOptionsView(store: store)
            }
        }
    }
}

private struct SSHAccessAdvancedOptionsView: View {
    @ObservedObject var store: MaintenanceStore

    var body: some View {
        Toggle(L10n.string("toggle.no_wait"), isOn: $store.noWait)
    }
}

private struct RemoteMaintenanceAdvancedOptionsView: View {
    @ObservedObject var store: MaintenanceStore

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
            GridRow {
                Text(L10n.string("field.mount_wait"))
                    .foregroundStyle(.secondary)
                TextField(L10n.string("field.mount_wait"), text: $store.mountWait)
                    .frame(width: 150)
            }
            GridRow {
                Toggle(L10n.string("toggle.no_reboot"), isOn: $store.noReboot)
                    .disabled(!RebootExecutionOptionPolicy.allowsNoReboot(noWait: store.noWait))
                Toggle(L10n.string("toggle.no_wait"), isOn: $store.noWait)
                    .disabled(!RebootExecutionOptionPolicy.allowsNoWait(noReboot: store.noReboot))
            }
        }
    }
}
