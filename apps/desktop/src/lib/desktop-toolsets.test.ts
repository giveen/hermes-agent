import { describe, expect, it } from 'vitest'

import { isDesktopToolsetVisible } from './desktop-toolsets'

describe('isDesktopToolsetVisible', () => {
  it('hides platform-coupled and internal toolsets', () => {
      expect(isDesktopToolsetVisible(name)).toBe(false)
    }
  })

  it('keeps ordinary user-facing toolsets', () => {
    for (const name of ['web', 'browser', 'terminal', 'file', 'memory', 'vision', 'image_gen']) {
      expect(isDesktopToolsetVisible(name)).toBe(true)
    }
  })
})
