import SwiftUI

struct OverviewTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    @ObservedObject var appStore: AppStore

    var body: some View {
        let summary = session.summary(for: profile)
        let presentation = DeviceDashboardOverviewPresentation(
            summary: summary,
            currentCheckupSummary: session.doctorStore.summary
        )

        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let warning = presentation.hostWarning {
                    WarningBanner(warning: warning)
                }

                DashboardHeaderView(presentation: presentation.header)

                DashboardPrimaryActionStrip(
                    primaryAction: presentation.primaryAction,
                    secondaryActions: presentation.secondaryActions,
                    performPrimary: {
                        session.performPrimaryAction(presentation.primaryAction, profile: profile)
                    },
                    performSecondary: { action in
                        session.performSecondaryAction(action, profile: profile)
                    }
                )

                if presentation.requiresPasswordReplacement || session.isReplacingPassword {
                    PasswordReplacementView(profile: profile, session: session)
                }

                if let passwordError = session.passwordError {
                    Text(passwordError)
                        .foregroundStyle(.red)
                }

                VStack(alignment: .leading, spacing: 10) {
                    ForEach(presentation.healthSections) { section in
                        DashboardHealthSectionView(section: section) { action in
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
                    Text(presentation.host)
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
        Label(status.title, systemImage: status.systemImage)
            .font(.caption.weight(.medium))
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(.quaternary)
            .clipShape(Capsule())
    }
}

private struct DashboardPrimaryActionStrip: View {
    let primaryAction: DashboardPrimaryAction
    let secondaryActions: [DashboardSecondaryAction]
    let performPrimary: () -> Void
    let performSecondary: (DashboardSecondaryAction) -> Void

    var body: some View {
        HStack(spacing: 8) {
            DashboardPrimaryActionButton(action: primaryAction, perform: performPrimary)

            ForEach(secondaryActions) { action in
                Button {
                    performSecondary(action)
                } label: {
                    Label(action.title, systemImage: action.systemImage)
                }
            }
        }
    }
}

private struct DashboardPrimaryActionButton: View {
    let action: DashboardPrimaryAction
    let perform: () -> Void

    var body: some View {
        Button(action: perform) {
            Label(action.title, systemImage: action.systemImage)
        }
        .buttonStyle(.borderedProminent)
    }
}

private struct PasswordReplacementView: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(L10n.string("dashboard.password.title"))
                .font(.headline)
            HStack {
                SecureField(L10n.string("dashboard.replacement_password"), text: $session.replacementPassword)
                    .onSubmit {
                        Task { @MainActor in
                            await session.saveReplacementPassword(for: profile)
                        }
                    }
                Button {
                    Task { @MainActor in
                        await session.saveReplacementPassword(for: profile)
                    }
                } label: {
                    Label(L10n.string("dashboard.action.save_password"), systemImage: "key")
                }
                .disabled(session.replacementPassword.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
    }
}

private struct DashboardHealthSectionView: View {
    let section: DashboardHealthSection
    let performAction: (DashboardSecondaryAction) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(section.title)
                .font(.headline)
            ForEach(section.rows) { row in
                HStack(alignment: .top, spacing: 10) {
                    Label(row.status.title, systemImage: row.status.systemImage)
                        .font(.caption.weight(.medium))
                        .labelStyle(.iconOnly)
                        .frame(width: 18)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text(row.title)
                                .font(.body.weight(.medium))
                            Spacer()
                            if let action = row.action {
                                Button {
                                    performAction(action)
                                } label: {
                                    Label(action.title, systemImage: action.systemImage)
                                }
                                .controlSize(.small)
                            }
                        }
                        Text(row.detail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(10)
                .background(Color.secondary.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }
        }
    }
}
