import SwiftUI

private enum OverviewLayout {
    static let actionIconSize: CGFloat = 16
    static let healthRowMinHeight: CGFloat = 64
    static let healthStatusIconSize: CGFloat = 30
    static let healthStatusSymbolSize: CGFloat = 18
    static let healthActionSlotMinWidth: CGFloat = 144
}

struct OverviewTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    @ObservedObject var appStore: AppStore

    var body: some View {
        let summary = session.summary(for: profile)
        let presentation = DeviceDashboardOverviewPresentation(
            summary: summary,
            currentCheckupSummary: session.doctorStore.summary,
            reachabilitySnapshot: appStore.reachabilityStore.snapshot(for: profile),
            isReachabilityRunning: appStore.reachabilityStore.isRunning(profile: profile)
        )

        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let warning = presentation.hostWarning {
                    WarningBanner(warning: warning)
                }

                DashboardHeaderView(presentation: presentation.header)

                DashboardPrimaryActionStrip(
                    primaryAction: presentation.primaryAction,
                    isPrimaryActionEnabled: presentation.isPrimaryActionEnabled,
                    secondaryActions: presentation.secondaryActions,
                    isSecondaryActionEnabled: presentation.isEnabled,
                    performPrimary: {
                        session.performPrimaryAction(presentation.primaryAction, profile: profile)
                    },
                    performSecondary: { action in
                        session.performSecondaryAction(action, profile: profile)
                    }
                )

                VStack(alignment: .leading, spacing: 10) {
                    ForEach(presentation.healthSections) { section in
                        DashboardHealthSectionView(section: section, isActionEnabled: presentation.isEnabled) { action in
                            session.performSecondaryAction(action, profile: profile)
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct DashboardHeaderView: View {
    let presentation: DeviceDashboardHeaderPresentation

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(presentation.title)
                        .font(.title2.weight(.semibold))
                    Text(presentation.connectionTarget)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                StatusBadge(status: presentation.status)
            }

            HStack(spacing: 12) {
                Label(presentation.lastChecked, systemImage: "clock")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(L10n.string("dashboard.header.last_checked"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            SummaryGrid(rows: presentation.rows.map { ($0.label, $0.value) })
        }
    }
}

private struct StatusBadge: View {
    let status: DeviceDisplayStatus

    var body: some View {
        Label {
            Text(status.title)
        } icon: {
            Image(systemName: status.systemImage)
                .frame(width: OverviewLayout.actionIconSize, height: OverviewLayout.actionIconSize)
        }
            .font(.caption.weight(.medium))
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(.quaternary)
            .clipShape(Capsule())
    }
}

private struct DashboardPrimaryActionStrip: View {
    let primaryAction: DashboardPrimaryAction
    let isPrimaryActionEnabled: Bool
    let secondaryActions: [DashboardSecondaryAction]
    let isSecondaryActionEnabled: (DashboardSecondaryAction) -> Bool
    let performPrimary: () -> Void
    let performSecondary: (DashboardSecondaryAction) -> Void

    var body: some View {
        HStack(spacing: 8) {
            DashboardPrimaryActionButton(action: primaryAction, perform: performPrimary)
                .disabled(!isPrimaryActionEnabled)

            ForEach(secondaryActions) { action in
                Button {
                    performSecondary(action)
                } label: {
                    DashboardActionLabel(title: action.title, systemImage: action.systemImage)
                }
                .disabled(!isSecondaryActionEnabled(action))
            }
        }
    }
}

private struct DashboardPrimaryActionButton: View {
    let action: DashboardPrimaryAction
    let perform: () -> Void

    var body: some View {
        Button(action: perform) {
            DashboardActionLabel(title: action.title, systemImage: action.systemImage)
        }
        .buttonStyle(.borderedProminent)
    }
}

private struct DashboardActionLabel: View {
    let title: String
    let systemImage: String

    var body: some View {
        Label {
            Text(title)
                .lineLimit(1)
        } icon: {
            Image(systemName: systemImage)
                .frame(width: OverviewLayout.actionIconSize, height: OverviewLayout.actionIconSize)
        }
    }
}

private struct DashboardHealthSectionView: View {
    let section: DashboardHealthSection
    let isActionEnabled: (DashboardSecondaryAction) -> Bool
    let performAction: (DashboardSecondaryAction) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(section.title)
                .font(.headline)
            ForEach(section.rows) { row in
                HStack(alignment: .top, spacing: 10) {
                    DashboardHealthStatusIcon(status: row.status)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text(row.title)
                                .font(.body.weight(.medium))
                            Spacer()
                            DashboardHealthActionSlot(
                                action: row.action,
                                isActionEnabled: isActionEnabled,
                                performAction: performAction
                            )
                        }
                        AnimatedProgressText(message: row.detail, isRunning: row.status == .running)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(10)
                .frame(maxWidth: .infinity, minHeight: OverviewLayout.healthRowMinHeight, alignment: .topLeading)
                .background(Color.secondary.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }
        }
    }
}

private struct DashboardHealthActionSlot: View {
    let action: DashboardSecondaryAction?
    let isActionEnabled: (DashboardSecondaryAction) -> Bool
    let performAction: (DashboardSecondaryAction) -> Void

    var body: some View {
        Group {
            if let action {
                Button {
                    performAction(action)
                } label: {
                    DashboardActionLabel(title: action.title, systemImage: action.systemImage)
                }
                .controlSize(.small)
                .disabled(!isActionEnabled(action))
            } else {
                // Reserve real button metrics so rows without actions align with rows that have action buttons.
                Button {} label: {
                    DashboardActionLabel(
                        title: DashboardSecondaryAction.runCheckup.title,
                        systemImage: DashboardSecondaryAction.runCheckup.systemImage
                    )
                }
                    .controlSize(.small)
                    .hidden()
                    .accessibilityHidden(true)
                    .allowsHitTesting(false)
            }
        }
        .frame(
            minWidth: OverviewLayout.healthActionSlotMinWidth,
            alignment: .trailing
        )
    }
}

private struct DashboardHealthStatusIcon: View {
    let status: DashboardHealthStatus

    var body: some View {
        ZStack {
            Circle()
                .fill(statusColor.opacity(status == .unknown ? 0.10 : 0.14))
            icon
                .foregroundStyle(statusColor)
        }
            .frame(width: OverviewLayout.healthStatusIconSize, height: OverviewLayout.healthStatusIconSize)
            .accessibilityLabel(status.title)
    }

    @ViewBuilder
    private var icon: some View {
        if status == .running {
            OperationTimelineStateIcon(state: .running)
        } else {
            Image(systemName: status.systemImage)
                .font(.system(size: OverviewLayout.healthStatusSymbolSize, weight: .semibold))
        }
    }

    private var statusColor: Color {
        switch status {
        case .unknown:
            return .secondary
        case .good:
            return .green
        case .warning:
            return .orange
        case .failed:
            return .red
        case .running:
            return .accentColor
        }
    }
}
