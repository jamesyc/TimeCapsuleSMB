import AppKit
import SwiftUI

struct MaintenanceTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let showDiagnostics: () -> Void
    let diagnosticsText: () -> String

    var body: some View {
        let store = session.maintenanceStore
        let presentation = MaintenanceDashboardPresentation(store: store, profile: profile)

        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text(L10n.string("dashboard.tab.maintenance"))
                    .font(.title2.weight(.semibold))

                MaintenanceCardsView(cards: presentation.cards) { workflow in
                    store.selectedWorkflow = workflow
                }

                MaintenanceDetailView(
                    presentation: presentation.detail,
                    store: store,
                    performAction: { action in
                        session.performMaintenanceAction(action, profile: profile, showDiagnostics: showDiagnostics)
                    },
                    chooseFirmwareTemplate: {
                        chooseFirmwareTemplate(store: store.bootHookStore)
                    }
                )

                if let error = presentation.detail.error {
                    ErrorRecoveryView(
                        error: error,
                        diagnosticsText: diagnosticsText
                    ) { recoveryAction($0, error: error) }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func recoveryAction(_ kind: RecoveryActionKind, error: BackendErrorViewModel) -> (() -> Void)? {
        kind == .diagnostics ? { showDiagnostics() } : session.recoveryAction(kind, error: error, profile: profile)
    }

    private func chooseFirmwareTemplate(store: BootHookStore) {
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
    let select: (MaintenanceWorkflow) -> Void
    // The tallest card's content: every card takes it as its minimum, so the
    // cards stay even when one subtitle wraps further than the rest.
    @State private var contentHeight: CGFloat = 0

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 190), spacing: 10, alignment: .top)], alignment: .leading, spacing: 10) {
            ForEach(cards) { card in
                Button {
                    select(card.workflow)
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
    let chooseFirmwareTemplate: () -> Void

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
            AnimatedProgressText(message: presentation.statusMessage, isRunning: presentation.isRunning)
                .font(.callout)
                .foregroundStyle(.secondary)

            ForEach(presentation.warnings, id: \.self) { warning in
                MaintenanceWarningLabel(text: warning)
            }

            switch presentation.workflow {
            case .fsck:
                FsckTargetListView(store: store)
            case .bootHook:
                DisclosureGroup {
                    BootHookFirmwareOptionsView(store: store.bootHookStore, chooseFirmwareTemplate: chooseFirmwareTemplate)
                } label: {
                    Label(L10n.string("flash.options.apple_firmware"), systemImage: "gearshape")
                        .font(.subheadline.weight(.medium))
                }
            case .sshAccess, .activate, .uninstall, .wipeDisk:
                EmptyView()
            }

            ForEach(presentation.actionRows, id: \.self) { row in
                WrappingHStack {
                    ForEach(row) { action in
                        MaintenanceActionButton(
                            action: action,
                            title: presentation.title(for: action),
                            isEnabled: presentation.isEnabled(action),
                            perform: performAction
                        )
                    }
                }
            }

            if !presentation.bootHookRows.isEmpty {
                BootHookRowsView(rows: presentation.bootHookRows)
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

            // Wipe Internal Disk mounts nothing and always restarts and waits;
            // the boot hook's options are its firmware choices above.
            if presentation.workflow != .wipeDisk, presentation.workflow != .bootHook {
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
    let title: String
    let isEnabled: Bool
    let perform: (MaintenanceUserAction) -> Void

    var body: some View {
        if action.isCommitAction {
            Button {
                perform(action)
            } label: {
                Label(title, systemImage: action.systemImage)
            }
            .buttonStyle(.borderedProminent)
            .disabled(!isEnabled)
        } else {
            Button {
                perform(action)
            } label: {
                Label(title, systemImage: action.systemImage)
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
                MaintenanceWarningLabel(text: warning)
            }
        }
    }
}

/// Lays out its views left to right and starts a new line when the next one
/// does not fit, so a row of long translated buttons wraps instead of
/// truncating their labels. A view wider than the whole row still truncates.
struct WrappingHStack: Layout {
    var spacing: CGFloat = 8

    private struct Line {
        var indices: [Int]
        var width: CGFloat
        var height: CGFloat
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let lines = lines(width: proposal.width ?? .infinity, subviews: subviews)
        return CGSize(
            width: lines.map(\.width).max() ?? 0,
            height: lines.map(\.height).reduce(0, +) + spacing * CGFloat(max(lines.count - 1, 0))
        )
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for line in lines(width: bounds.width, subviews: subviews) {
            var x = bounds.minX
            for index in line.indices {
                let size = size(of: subviews[index], width: bounds.width)
                subviews[index].place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
            y += line.height + spacing
        }
    }

    private func lines(width: CGFloat, subviews: Subviews) -> [Line] {
        var lines: [Line] = []
        for (index, subview) in subviews.enumerated() {
            let size = size(of: subview, width: width)
            if let last = lines.last, last.width + spacing + size.width <= width {
                lines[lines.count - 1].indices.append(index)
                lines[lines.count - 1].width += spacing + size.width
                lines[lines.count - 1].height = max(last.height, size.height)
            } else {
                lines.append(Line(indices: [index], width: size.width, height: size.height))
            }
        }
        return lines
    }

    private func size(of subview: LayoutSubview, width: CGFloat) -> CGSize {
        let ideal = subview.sizeThatFits(.unspecified)
        return ideal.width <= width ? ideal : subview.sizeThatFits(ProposedViewSize(width: width, height: nil))
    }
}

/// A warning in the normal text color with an orange icon: orange or yellow
/// text is too faint to read on a light background.
private struct MaintenanceWarningLabel: View {
    let text: String

    var body: some View {
        Label {
            Text(text)
        } icon: {
            Image(systemName: "exclamationmark.triangle")
                .foregroundStyle(.orange)
        }
        .font(.caption)
    }
}

/// The boot hook's backup, plan and write details, each value selectable.
private struct BootHookRowsView: View {
    let rows: [PresentationRow]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(rows) { row in
                HStack(alignment: .firstTextBaseline) {
                    Text(row.label)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text(row.value)
                        .multilineTextAlignment(.trailing)
                        .textSelection(.enabled)
                }
                .font(.caption)
            }
        }
    }
}

private struct BootHookFirmwareOptionsView: View {
    @ObservedObject var store: BootHookStore
    let chooseFirmwareTemplate: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField(L10n.string("field.firmware_version"), text: $store.firmwareVersion)
            HStack {
                TextField(L10n.string("field.firmware_template"), text: $store.firmwareTemplatePath)
                Button {
                    chooseFirmwareTemplate()
                } label: {
                    Label(L10n.string("flash.action.choose_template"), systemImage: "doc")
                }
            }
        }
        .textFieldStyle(.roundedBorder)
        // A plan is made for these values; changing them mid-run would leave
        // the plan on screen without them.
        .disabled(store.isBusy)
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
