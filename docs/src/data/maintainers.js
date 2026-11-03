/**
 * List the people shown on /maintainers/, in display order.
 *
 * Set `login` to the GitHub username; it drives the avatar and the profile link.
 * Set `name` to the login when the maintainer has no public display name.
 */
export const maintainers = [
  {login: 'sansuiyi', name: 'sansuiyi', role: 'Maintainer'},
];

/** Return the GitHub avatar URL for `login` at `size` pixels. */
export function avatarUrl(login, size) {
  return `https://avatars.githubusercontent.com/${login}?s=${size}`;
}
