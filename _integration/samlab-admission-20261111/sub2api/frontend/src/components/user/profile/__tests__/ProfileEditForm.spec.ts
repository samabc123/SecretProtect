import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import ProfileEditForm from '@/components/user/profile/ProfileEditForm.vue'

const { updateProfileMock, showErrorMock, authState } = vi.hoisted(() => ({
  updateProfileMock: vi.fn(),
  showErrorMock: vi.fn(),
  authState: { user: { username: 'alice' } },
}))

vi.mock('@/api', () => ({
  userAPI: { updateProfile: updateProfileMock },
}))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authState,
}))
vi.mock('@/stores/app', () => ({
  useAppStore: () => ({ showError: showErrorMock }),
}))
vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

describe('ProfileEditForm', () => {
  it('shows the username as read-only', () => {
    const wrapper = mount(ProfileEditForm, { props: { initialUsername: 'alice' } })

    expect(wrapper.get('[data-testid="profile-username-readonly"]').text()).toBe('alice')
    expect(wrapper.find('#username').exists()).toBe(false)
    expect(wrapper.find('form').exists()).toBe(false)
  })

  it('does not call the profile update API', () => {
    mount(ProfileEditForm, { props: { initialUsername: 'alice' } })

    expect(updateProfileMock).not.toHaveBeenCalled()
    expect(showErrorMock).not.toHaveBeenCalled()
    expect(authState.user.username).toBe('alice')
  })
})
