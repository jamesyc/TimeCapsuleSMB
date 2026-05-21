import SwiftUI

struct FlashBootHookSection: View {
    let profile: DeviceProfile
    @StateObject private var store = FlashWorkflowStore()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Persistent NetBSD4 Boot Hook")
                        .font(.headline)
                    Text(store.eligibilityMessage)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Label(store.state.title, systemImage: "lock")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack {
                Button("Back Up and Inspect") {}
                    .disabled(true)
                Button("Patch Boot Hook") {}
                    .disabled(true)
                Button("Restore Apple Firmware") {}
                    .disabled(true)
            }
        }
        .onAppear {
            store.refresh(profile: profile)
        }
        .onChange(of: profile.id) { _ in
            store.refresh(profile: profile)
        }
    }
}
