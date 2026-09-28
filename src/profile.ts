export interface LocalProfile {
  name: string
  createdAt: string
  updatedAt?: string
  avatarDataUrl?: string
}

export const PROFILE_STORAGE_KEY = 'vequo.ops.profile.v1'

