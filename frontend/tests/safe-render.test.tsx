import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { TagPills } from '../src/components/ui'

describe('structured rendering', () => {
  it('renders an untrusted tag as text instead of executable markup', () => {
    const { container } = render(<TagPills tags={['<script>window.pwned = true</script>']} />)
    expect(screen.getByText('<script>window.pwned = true</script>')).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
  })
})
