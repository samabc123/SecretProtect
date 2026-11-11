import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, mount } from '@vue/test-utils'
import ProfileEditForm from '../ProfileEditForm.vue'

const mocks = vi.hoisted(() => ({ updateProfile: vi.fn(), state: { user: { username: 'alice' } } }))
vi.mock('@/api', () => ({ userAPI: { updateProfile: mocks.updateProfile } }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => mocks.state }))
vi.mock('@/stores/app', () => ({ useAppStore: () => ({ showError: vi.fn(), showSuccess: vi.fn() }) }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
enableAutoUnmount(afterEach)
beforeEach(() => {
  vi.resetAllMocks()
  mocks.state.user = { username: 'alice' }
})

describe('profile username draft', () => {
  it('renders the username without an editable input', () => {
    const wrapper = mount(ProfileEditForm, {
      props: { initialUsername: mocks.state.user.username },
    })

    expect(wrapper.get('[data-testid="profile-username-readonly"]').text()).toBe('alice')
    expect(wrapper.find('#username').exists()).toBe(false)
    expect(wrapper.find('form').exists()).toBe(false)
  })

  it('does not submit username changes through the profile API', () => {
    mount(ProfileEditForm, {
      props: { initialUsername: mocks.state.user.username },
    })

    expect(mocks.updateProfile).not.toHaveBeenCalled()
  })

  it('shows the fallback when no username is provided', () => {
    const wrapper = mount(ProfileEditForm, { props: { initialUsername: '' } })

    expect(wrapper.get('[data-testid="profile-username-readonly"]').text()).toBe('-')
  })
})
