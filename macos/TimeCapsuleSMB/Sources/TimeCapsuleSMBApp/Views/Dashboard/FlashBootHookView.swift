import SwiftUI

struct FlashBootHookSection: View {
    let profile: DeviceProfile
    @StateObject private var store = FlashWorkflowStore()

    var body: some View {
        let presentation = FlashPresentation(state: store.state, message: store.eligibilityMessage)
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text(presentation.title)
                        .font(.headline)
                    Text(presentation.message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Label(presentation.stateTitle, systemImage: "lock")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack {
                ForEach(presentation.actions) { action in
                    Button(action.title) {}
                        .disabled(!presentation.isEnabled(action))
                }
            }
        }
        .onAppear {
            store.refresh(profile: profile)
        }
        .onChange(of: profile.id) { _, _ in
            store.refresh(profile: profile)
        }
    }
}
