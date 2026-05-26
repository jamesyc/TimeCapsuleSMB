import SwiftUI

private enum OverviewLayout {
    static let actionIconSize: CGFloat = 16
    static let healthRowMinHeight: CGFloat = 64
    static let healthStatusIconSize: CGFloat = 18
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
                    Image(systemName: row.status.systemImage)
                        .font(.caption.weight(.medium))
                        .accessibilityLabel(row.status.title)
                        .frame(width: OverviewLayout.healthStatusIconSize, height: OverviewLayout.healthStatusIconSize)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text(row.title)
                                .font(.body.weight(.medium))
                            Spacer()
                            if let action = row.action {
                                Button {
                                    performAction(action)
                                } label: {
                                    DashboardActionLabel(title: action.title, systemImage: action.systemImage)
                                }
                                .controlSize(.small)
                                .disabled(!isActionEnabled(action))
                            }
                        }
                        Text(row.detail)
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
